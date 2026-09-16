from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "pr_ci_self_heal.py"
spec = importlib.util.spec_from_file_location("pr_ci_self_heal", MODULE)
assert spec and spec.loader
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class PrCiSelfHealTests(unittest.TestCase):
    def make_repo(self) -> Path:
        root = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "CI Test"], check=True)
        (root / "README.md").write_text("# Teste\n\nVersão: 1.6.1\n", encoding="utf-8")
        (root / "payload.txt").write_text("v1\n", encoding="utf-8")
        (root / "MANIFEST.json").write_text(
            json.dumps({"version": "1.6.1", "generated_at": "old", "files": []}) + "\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-m", "baseline"], check=True, capture_output=True)
        return root

    def test_regenerate_is_idempotent_after_manifest_commit(self):
        root = self.make_repo()
        changed, first = m.regenerate_if_needed(root, {"README.md", "payload.txt"})
        self.assertTrue(changed)
        self.assertEqual([item["path"] for item in first["files"]], ["README.md", "payload.txt"])
        subprocess.run(["git", "-C", str(root), "add", "MANIFEST.json"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-m", "manifest"], check=True, capture_output=True)
        changed_again, second = m.regenerate_if_needed(root, {"README.md", "payload.txt"})
        self.assertFalse(changed_again)
        self.assertEqual(first["files"], second["files"])

    def test_payload_change_updates_only_manifest_when_repo_is_recleaned(self):
        root = self.make_repo()
        changed, _ = m.regenerate_if_needed(root, {"README.md", "payload.txt"})
        self.assertTrue(changed)
        subprocess.run(["git", "-C", str(root), "add", "MANIFEST.json"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-m", "manifest"], check=True, capture_output=True)
        (root / "payload.txt").write_text("v2\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "payload.txt"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-m", "payload"], check=True, capture_output=True)
        m.verify_clean(root)
        changed, _ = m.regenerate_if_needed(root, {"README.md", "payload.txt"})
        self.assertTrue(changed)
        self.assertEqual(m.changed_paths(root), ["MANIFEST.json"])

    def test_verify_clean_blocks_dirty_target(self):
        root = self.make_repo()
        (root / "payload.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "target_not_clean"):
            m.verify_clean(root)

    def test_verify_expected_head_detects_concurrency(self):
        root = self.make_repo()
        with self.assertRaisesRegex(RuntimeError, "head_changed"):
            m.verify_expected_head(root, "0" * 40)


if __name__ == "__main__":
    unittest.main()

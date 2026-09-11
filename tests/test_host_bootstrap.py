from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import install_command_gateway_host as hb


class HostBootstrapTests(unittest.TestCase):
    def make_bundle(self, root: Path, version: str = "1.5.1") -> Path:
        bundle = root / "bundle"
        entries = []
        for source in hb.RUNTIME_MAP:
            payload = f"payload:{source}\n".encode("utf-8")
            target = bundle / source
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            entries.append({"path": source, "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)})
        (bundle / "MANIFEST.json").write_text(json.dumps({"version": version, "files": entries}), encoding="utf-8")
        return bundle

    def test_validate_commit_requires_full_sha(self) -> None:
        valid = "a" * 40
        self.assertEqual(hb.validate_commit(valid.upper()), valid)
        with self.assertRaises(hb.HostBootstrapError):
            hb.validate_commit("abc123")

    def test_verify_bundle_rejects_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = self.make_bundle(root)
            source = next(iter(hb.RUNTIME_MAP))
            (bundle / source).write_text("tampered\n", encoding="utf-8")
            with self.assertRaises(hb.HostBootstrapError):
                hb.verify_bundle(bundle)

    def test_install_bundle_creates_verified_runtime_and_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = self.make_bundle(root)
            install_root = root / "install"
            work_root = root / "workers"
            receipt = hb.install_bundle(bundle, install_root, work_root, "b" * 40)
            self.assertEqual(receipt["result"], "HOST_BOOTSTRAP_OK")
            self.assertEqual(receipt["rules_version"], "1.5.1")
            self.assertTrue(work_root.is_dir())
            self.assertTrue((install_root / "install-receipt.json").is_file())
            for dest in hb.RUNTIME_MAP.values():
                self.assertTrue((install_root / dest).is_file())

    def test_reinstall_creates_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = self.make_bundle(root)
            install_root = root / "install"
            work_root = root / "workers"
            hb.install_bundle(bundle, install_root, work_root, "c" * 40)
            second = hb.install_bundle(bundle, install_root, work_root, "c" * 40)
            self.assertIsNotNone(second["backup_dir"])
            self.assertTrue(Path(second["backup_dir"]).is_dir())

    def test_prepare_validation_repo_uses_exact_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            import subprocess
            subprocess.run(["git", "init"], cwd=source, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "host-bootstrap@example.invalid"], cwd=source, check=True)
            subprocess.run(["git", "config", "user.name", "Host Bootstrap"], cwd=source, check=True)
            (source / "README.md").write_text("ok\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=source, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=source, check=True, capture_output=True)
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, check=True, text=True, capture_output=True).stdout.strip()
            git_config = root / "global.gitconfig"
            with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(git_config)}):
                target, observed = hb.prepare_validation_repo(root / "workers", head, str(source))
                listed = subprocess.run(["git", "config", "--global", "--get-all", "safe.directory"],
                                        check=True, text=True, capture_output=True).stdout.splitlines()
            self.assertEqual(observed, head)
            self.assertTrue((target / ".git").exists())
            self.assertIn(str(target.resolve()).replace("\\", "/"), listed)
            self.assertNotIn("*", listed)


if __name__ == "__main__":
    unittest.main()

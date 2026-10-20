from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]

def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

gateway = load("owner_risk3_gateway_for_specific_test", ROOT / "scripts" / "owner_risk3_gateway.py")
sys.modules["owner_risk3_gateway"] = gateway
module = load("set_owner_risk3_action", ROOT / "scripts" / "set_owner_risk3_action.py")


class SpecificRisk3ActionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        subprocess.run(["git", "init", str(self.root)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "Test"], check=True)
        scripts = self.root / "scripts"
        scripts.mkdir()
        self.script = scripts / "task.py"
        self.script.write_text("print('ok')\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "scripts/task.py"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-m", "base"], check=True, capture_output=True)
        self.head = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True
        ).stdout.strip()
        self.config = Path(self.temp.name) / "owner-risk3-exceptions.local.json"
        self.config.write_text(json.dumps({
            "version": 1,
            "enabled": True,
            "owner_fingerprint": gateway.owner_fingerprint(),
            "actions": {},
        }), encoding="utf-8")
        if os.name != "nt":
            os.chmod(self.config, 0o600)

    def tearDown(self):
        self.temp.cleanup()

    def authorize(self, **overrides):
        values = {
            "config_path": self.config,
            "action_id": "noteri.runner.install.dev",
            "scope": "repo://noteri-runtime/runner-install/dev",
            "environment": "dev",
            "repo_root": self.root,
            "expected_head": self.head,
            "script": Path("scripts/task.py"),
            "args": ["--confirm", "INSTALL-NOTERI-RUNTIME-RUNNER"],
            "expires_minutes": 30,
            "authorization_ref": "chat-authorization-runner-install-20261020",
            "confirm": module.CONFIRM,
        }
        values.update(overrides)
        return module.authorize(**values)

    def test_authorizes_exact_clean_head_and_persists_binding(self):
        result = self.authorize()
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        action = saved["actions"]["noteri.runner.install.dev"]
        self.assertEqual(result["status"], "authorized")
        self.assertEqual(action["cwd"], str(self.root.resolve()))
        self.assertEqual(action["git_head"], self.head)
        self.assertEqual(action["command"][0], sys.executable)
        self.assertEqual(Path(action["command"][1]), self.script.resolve())
        self.assertEqual(len(action["script_sha256"]), 64)
        self.assertFalse(result["broad_development_mode_changed"])
        binding = gateway.validate_explicit_binding(action, self.root)
        self.assertEqual(binding["git_head"], self.head)

    def test_replay_is_detected_without_enabling_broad_mode(self):
        first = self.authorize()
        second = self.authorize()
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertNotIn("development_mode", saved)

    def test_wrong_head_is_rejected(self):
        with self.assertRaisesRegex(module.SpecificActionError, "head_mismatch"):
            self.authorize(expected_head="b" * 40)

    def test_dirty_repo_is_rejected(self):
        (self.root / "dirty.txt").write_text("dirty", encoding="utf-8")
        with self.assertRaisesRegex(module.SpecificActionError, "worktree_not_clean"):
            self.authorize()

    def test_untracked_script_is_rejected(self):
        extra = self.root / "scripts" / "untracked.py"
        extra.write_text("print('x')\n", encoding="utf-8")
        with self.assertRaisesRegex(module.SpecificActionError, "worktree_not_clean"):
            self.authorize(script=Path("scripts/untracked.py"))

    def test_confirmation_and_window_are_bounded(self):
        with self.assertRaisesRegex(module.SpecificActionError, "confirmation_invalid"):
            self.authorize(confirm="WRONG")
        with self.assertRaisesRegex(module.SpecificActionError, "expires_minutes_invalid"):
            self.authorize(expires_minutes=61)

    def test_explicit_binding_rejects_other_cwd(self):
        self.authorize()
        action = json.loads(self.config.read_text(encoding="utf-8"))["actions"]["noteri.runner.install.dev"]
        other = Path(self.temp.name) / "other"
        other.mkdir()
        with self.assertRaisesRegex(gateway.Risk3Error, "cwd"):
            gateway.validate_explicit_binding(action, other)

    def test_explicit_binding_rejects_head_change(self):
        self.authorize()
        action = json.loads(self.config.read_text(encoding="utf-8"))["actions"]["noteri.runner.install.dev"]
        (self.root / "next.txt").write_text("next", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "next.txt"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-m", "next"], check=True, capture_output=True)
        with self.assertRaisesRegex(gateway.Risk3Error, "git_head"):
            gateway.validate_explicit_binding(action, self.root)


if __name__ == "__main__":
    unittest.main()

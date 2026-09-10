from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import command_gateway as cg


class CommandGatewayTests(unittest.TestCase):
    def test_pattern_match_accepts_descendant_and_wildcard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertTrue(cg.pattern_match(str(root / "repo" / "sub"), str(root / "repo")))
            self.assertTrue(cg.pattern_match(str(root / "wt-one" / "sub"), str(root / "wt-*")))

    def test_sensitive_reference_blocks_env_and_token(self) -> None:
        policy = {"denied_names": [".env", ".env.*"], "denied_segments": [".ssh"]}
        self.assertTrue(cg.sensitive_reference(["git", "diff", "--", ".env"], policy))
        self.assertTrue(cg.sensitive_reference(["token=abc123"], policy))

    def test_redaction_masks_secret(self) -> None:
        masked = cg.redact("password=abc token=xyz Bearer abc.def")
        self.assertNotIn("abc", masked); self.assertNotIn("xyz", masked)

    def test_validate_command_blocks_risk3_and_git_push(self) -> None:
        policy = {"allowed_executables": ["git"], "blocked_executables": [],
                  "denied_names": [], "denied_segments": [], "deny_inline_code": True}
        with self.assertRaises(cg.GatewayError):
            cg.validate_command(["git", "status"], 3, policy)
        with self.assertRaises(cg.GatewayError):
            cg.validate_command(["git", "push"], 2, policy)

    def test_load_policy_rejects_wrong_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            path.write_text(json.dumps({"version": 99}), encoding="utf-8")
            with self.assertRaises(cg.GatewayError):
                cg.load_policy(path)


if __name__ == "__main__":
    unittest.main()

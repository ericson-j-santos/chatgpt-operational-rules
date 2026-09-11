from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
POLICY = ROOT / "config" / "command-gateway.policy.json"
sys.path.insert(0, str(SCRIPTS))

import command_gateway as cg


class CommandGatewayTests(unittest.TestCase):
    def test_pattern_match_accepts_descendant_and_wildcard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertTrue(cg.pattern_match(str(root / "repo" / "sub"), str(root / "repo")))
            self.assertTrue(cg.pattern_match(str(root / "wt-one" / "sub"), str(root / "wt-*")))

    def test_operational_policy_allows_only_dedicated_worker_namespace(self) -> None:
        policy = cg.load_policy(POLICY)
        allowed = policy["allowed_roots"]
        self.assertIn(r"C:\dev\chatgpt-workers\*", allowed)
        self.assertNotIn(r"C:\Users\Windows\portal-portabilidade", allowed)
        self.assertNotIn(r"D:\portal-portabilidade", allowed)

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

    def test_git_status_warning_is_not_accepted_as_complete_state(self) -> None:
        result = subprocess.CompletedProcess(["git", "status"], 0, "", "warning: permission denied")
        original = cg.run_capture
        responses = iter([
            subprocess.CompletedProcess(["git"], 0, "/tmp/repo\n", ""),
            subprocess.CompletedProcess(["git"], 0, "a" * 40 + "\n", ""),
            subprocess.CompletedProcess(["git"], 0, "main\n", ""),
            result,
            subprocess.CompletedProcess(["git"], 0, "", ""),
        ])
        try:
            cg.run_capture = lambda *args, **kwargs: next(responses)
            with self.assertRaises(cg.GatewayError) as ctx:
                cg.git_state(Path("/tmp/repo"), True)
            self.assertEqual(ctx.exception.exit_code, cg.EXIT_STATE_CHANGED)
        finally:
            cg.run_capture = original


    def test_git_untracked_exclude_keeps_other_untracked_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo=Path(tmp); subprocess.run(["git","init"],cwd=repo,check=True,capture_output=True)
            subprocess.run(["git","config","user.email","test@example.invalid"],cwd=repo,check=True)
            subprocess.run(["git","config","user.name","Test"],cwd=repo,check=True)
            (repo/".gitignore").write_text(".tmp/\n",encoding="utf-8")
            (repo/"a.txt").write_text("a",encoding="utf-8")
            subprocess.run(["git","add","."],cwd=repo,check=True); subprocess.run(["git","commit","-m","base"],cwd=repo,check=True,capture_output=True)
            policy={"git_untracked_excludes":[".tmp/**"]}
            before=cg.git_state(repo,True,policy); (repo/".tmp").mkdir(); (repo/".tmp/x").write_text("x")
            ignored=cg.git_state(repo,True,policy); self.assertEqual(before.status_digest,ignored.status_digest)
            (repo/"outside.txt").write_text("x"); outside=cg.git_state(repo,True,policy)
            self.assertNotEqual(ignored.status_digest,outside.status_digest)

    def test_load_policy_rejects_wrong_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            path.write_text(json.dumps({"version": 99}), encoding="utf-8")
            with self.assertRaises(cg.GatewayError):
                cg.load_policy(path)


if __name__ == "__main__":
    unittest.main()

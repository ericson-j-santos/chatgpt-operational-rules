from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import command_gateway as cg
import session_bootstrap as sb


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=str(cwd), text=True, capture_output=True, check=True)
    return completed.stdout.strip()


class SessionBootstrapTests(unittest.TestCase):
    def make_repo(self, root: Path, name: str) -> Path:
        repo = root / name
        repo.mkdir()
        git(repo, "init")
        git(repo, "config", "user.email", "session-test@example.invalid")
        git(repo, "config", "user.name", "Session Test")
        (repo / "baseline.txt").write_text("baseline\n", encoding="utf-8")
        git(repo, "add", "baseline.txt")
        git(repo, "commit", "-m", "baseline")
        return repo

    def policy(self, root: Path) -> dict:
        return {
            "version": 1,
            "allowed_roots": [str(root)],
            "denied_roots": [str(root / "blocked")],
            "denied_segments": [".ssh"],
            "denied_names": [".env"],
            "allowed_executables": ["git", "python", "python3"],
            "blocked_executables": ["cmd", "powershell", "bash", "sh"],
            "deny_inline_code": True,
            "require_git_repo": True,
            "risk2_requires_clean_tree": True,
            "max_timeout_seconds": 120,
            "state_dir": str(root / "state"),
            "git_untracked_excludes": [],
            "require_session_bootstrap": True,
            "worktree_root": str(root),
            "worktree_prefix": "wt-chat-",
        }

    def test_invalid_session_id_is_blocked(self) -> None:
        with self.assertRaises(cg.GatewayError) as ctx:
            sb.validate_session_id("../bad")
        self.assertEqual(ctx.exception.exit_code, sb.EXIT_SESSION_REQUIRED)

    def test_same_session_is_idempotent_and_other_repo_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo1 = self.make_repo(root, "repo1")
            repo2 = self.make_repo(root, "repo2")
            policy = self.policy(root)
            first, result1 = sb.reserve(repo1, policy, "chat-001", "corr-1")
            second, result2 = sb.reserve(repo1, policy, "chat-001", "corr-2")
            self.assertEqual(result1, "reserved")
            self.assertEqual(result2, "already_reserved")
            self.assertEqual(first["reserved_worktree"], second["reserved_worktree"])
            with self.assertRaises(cg.GatewayError) as ctx:
                sb.reserve(repo2, policy, "chat-001", "corr-3")
            self.assertEqual(ctx.exception.exit_code, sb.EXIT_SESSION_CONFLICT)

    def test_policy_must_explicitly_require_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root, "repo")
            policy = self.policy(root)
            policy["require_session_bootstrap"] = False
            with self.assertRaises(cg.GatewayError) as ctx:
                sb.reserve(repo, policy, "chat-002", "corr")
            self.assertEqual(ctx.exception.exit_code, sb.EXIT_SESSION_REQUIRED)

    def test_materialize_creates_clean_detached_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root, "repo")
            policy = self.policy(root)
            reservation, _ = sb.reserve(repo, policy, "chat-003", "corr")
            updated, result = sb.materialize(reservation, policy, "corr")
            target = Path(updated["reserved_worktree"])
            self.assertEqual(result, "materialized")
            self.assertTrue(target.is_dir())
            state = cg.git_state(target, True, policy)
            self.assertIsNotNone(state)
            self.assertEqual(state.head, updated["base_head"])
            self.assertEqual(state.branch, "DETACHED")
            self.assertEqual(state.status_count, 0)

    def test_materialize_blocks_case_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root, "repo")
            policy = self.policy(root)
            reservation, _ = sb.reserve(repo, policy, "chat-004", "corr")
            original = cg.tracked_case_collisions
            try:
                cg.tracked_case_collisions = lambda _repo: [("A.txt", "a.txt")]
                with self.assertRaises(cg.GatewayError) as ctx:
                    sb.materialize(reservation, policy, "corr")
                self.assertEqual(ctx.exception.exit_code, cg.EXIT_STATE_CHANGED)
                self.assertIn("colidem por casing", str(ctx.exception))
            finally:
                cg.tracked_case_collisions = original


if __name__ == "__main__":
    unittest.main()

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
import session_preflight as sp


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), text=True,
        capture_output=True, check=True, timeout=20,
    )
    return result.stdout.strip()


def make_repo(root: Path, name: str) -> Path:
    repo = root / name
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "preflight-test@example.invalid")
    git(repo, "config", "user.name", "Preflight Test")
    (repo / "baseline.txt").write_text("baseline\n", encoding="utf-8", newline="\n")
    git(repo, "add", "baseline.txt")
    git(repo, "commit", "-m", "baseline")
    return repo


def make_policy(root: Path) -> tuple[dict, Path]:
    policy = {
        "version": 1,
        "rules_version": "1.4.0",
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
        "require_preflight_snapshot": True,
        "worktree_root": str(root),
        "worktree_prefix": "wt-chat-",
    }
    path = root / "policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8", newline="\n")
    return policy, path


class SessionPreflightTests(unittest.TestCase):
    def test_capture_creates_integrity_checked_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo = make_repo(root, "repo")
            policy, policy_path = make_policy(root)
            result = sp.preflight(repo, policy_path, "chat-001", "corr-001", False)
            self.assertEqual(result["result"], "BOOTSTRAP_OK")
            self.assertTrue(result["state_validated"])
            path = sp.snapshot_path(policy, "chat-001")
            persisted = sp.read_valid_snapshot(path)
            self.assertEqual(persisted["snapshot_sha256"], result["snapshot_sha256"])
            reservation = cg.enforce_session(policy, "chat-001", repo, 1)
            self.assertEqual(reservation["session_id"], "chat-001")

    def test_reserved_session_detects_base_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo = make_repo(root, "repo")
            _, policy_path = make_policy(root)
            sp.preflight(repo, policy_path, "chat-002", "corr-002", False)
            (repo / "drift.txt").write_text("drift", encoding="utf-8")
            with self.assertRaises(cg.GatewayError) as ctx:
                sp.preflight(repo, policy_path, "chat-002", "corr-003", False)
            self.assertEqual(ctx.exception.exit_code, cg.EXIT_STATE_CHANGED)

    def test_materialized_session_binds_risk2_to_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo = make_repo(root, "repo")
            policy, policy_path = make_policy(root)
            result = sp.preflight(repo, policy_path, "chat-003", "corr-004", True)
            worktree = Path(result["reserved_worktree"])
            self.assertEqual(result["reservation_status"], "materialized")
            self.assertTrue(worktree.is_dir())
            self.assertEqual(git(worktree, "status", "--porcelain"), "")
            cg.enforce_session(policy, "chat-003", worktree, 2)
            with self.assertRaises(cg.GatewayError) as base_ctx:
                cg.enforce_session(policy, "chat-003", repo, 2)
            self.assertEqual(base_ctx.exception.exit_code, cg.EXIT_SESSION_REQUIRED)
            with self.assertRaises(cg.GatewayError) as missing_ctx:
                cg.enforce_session(policy, None, worktree, 1)
            self.assertEqual(missing_ctx.exception.exit_code, cg.EXIT_SESSION_REQUIRED)

    def test_tampered_snapshot_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo = make_repo(root, "repo")
            policy, policy_path = make_policy(root)
            sp.preflight(repo, policy_path, "chat-004", "corr-005", False)
            path = sp.snapshot_path(policy, "chat-004")
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["rules_version"] = "tampered"
            path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
            with self.assertRaises(cg.GatewayError) as ctx:
                cg.enforce_session(policy, "chat-004", repo, 1)
            self.assertEqual(ctx.exception.exit_code, cg.EXIT_SESSION_REQUIRED)


if __name__ == "__main__":
    unittest.main()

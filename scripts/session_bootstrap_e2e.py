#!/usr/bin/env python3
"""E2E do bootstrap de sessão e reserva/materialização de worktree."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().with_name("session_bootstrap.py")


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), text=True, capture_output=True, check=True, timeout=20)
    return result.stdout.strip()


def run_cli(args: list[str], expected: int) -> subprocess.CompletedProcess[str]:
    result = subprocess.run([sys.executable, str(SCRIPT), *args], text=True, capture_output=True, check=False, timeout=40)
    if result.returncode != expected:
        raise AssertionError(
            f"retorno inesperado esperado={expected} atual={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


def make_repo(root: Path, name: str) -> Path:
    repo = root / name
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "session-e2e@example.invalid")
    git(repo, "config", "user.name", "Session E2E")
    (repo / "baseline.txt").write_text("baseline\n", encoding="utf-8", newline="\n")
    git(repo, "add", "baseline.txt")
    git(repo, "commit", "-m", "baseline")
    return repo


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="session-bootstrap-e2e-"))
    try:
        repo1 = make_repo(root, "repo1")
        repo2 = make_repo(root, "repo2")
        state = root / "state"
        policy = {
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
            "state_dir": str(state),
            "git_untracked_excludes": [],
            "require_session_bootstrap": True,
            "worktree_root": str(root),
            "worktree_prefix": "wt-chat-",
        }
        policy_path = root / "policy.json"
        policy_path.write_text(json.dumps(policy), encoding="utf-8", newline="\n")
        base = ["--policy", str(policy_path), "--repo", str(repo1), "--session-id", "chat-e2e-001"]

        first = run_cli(base, 0)
        first_payload = json.loads(first.stdout.splitlines()[-1])
        if first_payload.get("result") != "BOOTSTRAP_OK" or first_payload.get("state") != "reserved":
            raise AssertionError("primeiro bootstrap não criou reserva válida")
        target = Path(first_payload["reserved_worktree"])
        if target.exists():
            raise AssertionError("reserva não deve materializar worktree sem solicitação")

        second = run_cli(base, 0)
        if json.loads(second.stdout.splitlines()[-1]).get("reserve_result") != "already_reserved":
            raise AssertionError("bootstrap repetido não foi idempotente")

        materialized = run_cli([*base, "--materialize"], 0)
        payload = json.loads(materialized.stdout.splitlines()[-1])
        if payload.get("state") != "materialized" or not target.is_dir():
            raise AssertionError("worktree não foi materializado")
        if git(target, "rev-parse", "HEAD") != payload["base_head"]:
            raise AssertionError("HEAD do worktree diverge do SHA reservado")
        if git(target, "status", "--porcelain"):
            raise AssertionError("worktree materializado não está limpo")

        repeated = run_cli([*base, "--materialize"], 0)
        if json.loads(repeated.stdout.splitlines()[-1]).get("materialize_result") != "already_materialized":
            raise AssertionError("materialização repetida não foi idempotente")

        conflict = ["--policy", str(policy_path), "--repo", str(repo2), "--session-id", "chat-e2e-001"]
        run_cli(conflict, 26)
        run_cli(["--policy", str(policy_path), "--repo", str(repo1), "--session-id", "../bad"], 25)

        disabled = dict(policy)
        disabled["require_session_bootstrap"] = False
        disabled_path = root / "disabled.json"
        disabled_path.write_text(json.dumps(disabled), encoding="utf-8", newline="\n")
        run_cli(["--policy", str(disabled_path), "--repo", str(repo1), "--session-id", "chat-e2e-002"], 25)

        events = [json.loads(line) for line in (state / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        if not any(item.get("action") == "session_bootstrap" and item.get("result") == "BOOTSTRAP_OK" for item in events):
            raise AssertionError("bootstrap positivo não foi auditado")
        if sum(1 for item in events if item.get("result") == "BOOTSTRAP_BLOCKED") < 2:
            raise AssertionError("controles negativos não foram auditados")

        print("SESSION_BOOTSTRAP_E2E_OK positive=4 negative=3 reservation=idempotent worktree=isolated final_state=clean")
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

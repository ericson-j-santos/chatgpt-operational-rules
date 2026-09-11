#!/usr/bin/env python3
"""E2E do preflight automático e enforcement de sessão no gateway."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATEWAY = ROOT / "scripts" / "command_gateway.py"
PREFLIGHT = ROOT / "scripts" / "session_preflight.py"


def run(script: Path, args: list[str], expected: int) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-B", str(script), *args],
        text=True, capture_output=True, check=False, timeout=45,
    )
    if result.returncode != expected:
        raise AssertionError(
            f"retorno inesperado esperado={expected} atual={result.returncode}\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), text=True,
        capture_output=True, check=True, timeout=20,
    )
    return result.stdout.strip()


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "preflight-e2e@example.invalid")
    git(repo, "config", "user.name", "Preflight E2E")
    (repo / "baseline.txt").write_text("baseline\n", encoding="utf-8", newline="\n")
    git(repo, "add", "baseline.txt")
    git(repo, "commit", "-m", "baseline")
    return repo


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="session-preflight-e2e-"))
    try:
        repo = make_repo(root)
        state = root / "state"
        policy = {
            "version": 1,
            "rules_version": "1.4.0",
            "allowed_roots": [str(root)],
            "denied_roots": [str(root / "blocked")],
            "denied_segments": [".ssh"],
            "denied_names": [".env"],
            "allowed_executables": ["git", "python", "python3", Path(sys.executable).name],
            "blocked_executables": ["cmd", "powershell", "bash", "sh"],
            "deny_inline_code": False,
            "require_git_repo": True,
            "risk2_requires_clean_tree": True,
            "max_timeout_seconds": 120,
            "state_dir": str(state),
            "git_untracked_excludes": [],
            "require_session_bootstrap": True,
            "require_preflight_snapshot": True,
            "worktree_root": str(root),
            "worktree_prefix": "wt-chat-",
        }
        policy_path = root / "policy.json"
        policy_path.write_text(json.dumps(policy), encoding="utf-8", newline="\n")
        session = "chat-e2e-001"

        base_gateway = ["--policy", str(policy_path)]
        run(
            GATEWAY,
            [*base_gateway, "run", "--cwd", str(repo), "--risk", "1", "--", "git", "status", "--short"],
            25,
        )

        first = run(
            PREFLIGHT,
            ["--policy", str(policy_path), "--repo", str(repo), "--session-id", session,
             "--correlation-id", "preflight-e2e-reserve"],
            0,
        )
        reserved = json.loads(first.stdout.splitlines()[-1])
        if reserved.get("result") != "BOOTSTRAP_OK" or reserved.get("reservation_status") != "reserved":
            raise AssertionError("preflight de leitura não criou snapshot reservado válido")
        run(
            GATEWAY,
            [*base_gateway, "inspect", "--cwd", str(repo), "--session-id", session],
            0,
        )
        run(
            GATEWAY,
            [*base_gateway, "run", "--cwd", str(repo), "--session-id", session,
             "--risk", "2", "--", "git", "status", "--short"],
            25,
        )

        second = run(
            PREFLIGHT,
            ["--policy", str(policy_path), "--repo", str(repo), "--session-id", session,
             "--correlation-id", "preflight-e2e-materialize", "--materialize"],
            0,
        )
        materialized = json.loads(second.stdout.splitlines()[-1])
        worktree = Path(materialized["reserved_worktree"])
        if materialized.get("reservation_status") != "materialized" or not worktree.is_dir():
            raise AssertionError("preflight não materializou worktree da sessão")

        mutation = "__import__('pathlib').Path('controlled.txt').write_text('ok', encoding='utf-8')"
        run(
            GATEWAY,
            [*base_gateway, "run", "--cwd", str(worktree), "--session-id", session,
             "--risk", "2", "--", sys.executable, "-B", "-c", mutation],
            0,
        )
        if not (worktree / "controlled.txt").is_file():
            raise AssertionError("efeito de risco 2 no worktree não foi observado")
        if git(repo, "status", "--porcelain"):
            raise AssertionError("base foi alterada pelo trabalho isolado")

        snapshot = state / "snapshots" / f"{session}.json"
        tampered = json.loads(snapshot.read_text(encoding="utf-8"))
        tampered["rules_version"] = "tampered"
        snapshot.write_text(json.dumps(tampered), encoding="utf-8", newline="\n")
        run(
            GATEWAY,
            [*base_gateway, "inspect", "--cwd", str(worktree), "--session-id", session],
            25,
        )

        events = [
            json.loads(line) for line in (state / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        bound = [event for event in events if event.get("session_id") == session]
        if not bound:
            raise AssertionError("nenhuma evidência foi vinculada ao session_id")
        if not any(event.get("action") == "session_preflight" for event in bound):
            raise AssertionError("preflight não foi auditado")

        print(
            "SESSION_PREFLIGHT_E2E_OK positive=4 negative=3 "
            "snapshot=integrity_checked risk2=worktree_only base=unchanged"
        )
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

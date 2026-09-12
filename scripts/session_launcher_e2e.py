#!/usr/bin/env python3
"""E2E do Session Launcher sobre bootstrap, preflight e Gateway reais."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATEWAY = ROOT / "scripts" / "command_gateway.py"
LAUNCHER = ROOT / "scripts" / "session_launcher.py"


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
    git(repo, "config", "user.email", "launcher-e2e@example.invalid")
    git(repo, "config", "user.name", "Launcher E2E")
    (repo / "baseline.txt").write_text("baseline\n", encoding="utf-8", newline="\n")
    git(repo, "add", "baseline.txt")
    git(repo, "commit", "-m", "baseline")
    return repo


def write_policy(path: Path, root: Path, state: Path, version: str) -> None:
    policy = {
        "version": 1,
        "rules_version": version,
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
    path.write_text(json.dumps(policy), encoding="utf-8", newline="\n")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="session-launcher-e2e-"))
    previous_git_config = os.environ.get("GIT_CONFIG_GLOBAL")
    os.environ["GIT_CONFIG_GLOBAL"] = str(root / "global.gitconfig")
    try:
        repo = make_repo(root)
        state = root / "state"
        policy_path = root / "policy.json"
        write_policy(policy_path, root, state, "1.6.0")
        head = git(repo, "rev-parse", "HEAD")

        launched = run(
            LAUNCHER,
            [
                "--policy", str(policy_path),
                "--repo", str(repo),
                "--session-prefix", "reqsys",
                "--expected-head", head,
                "--correlation-id", "launcher-e2e-positive",
            ],
            0,
        )
        payload = json.loads(launched.stdout.splitlines()[-1])
        if payload.get("result") != "SESSION_LAUNCH_OK" or not payload.get("state_validated"):
            raise AssertionError("launcher não retornou aceite válido")
        session = payload["session_id"]
        target = Path(payload["target_path"])
        if not target.is_dir() or git(target, "rev-parse", "HEAD") != head:
            raise AssertionError("worktree lançado não corresponde ao HEAD esperado")
        if git(repo, "status", "--porcelain"):
            raise AssertionError("base foi alterada pelo launcher")

        run(
            GATEWAY,
            [
                "--policy", str(policy_path), "inspect",
                "--cwd", str(target), "--session-id", session,
            ],
            0,
        )
        run(
            GATEWAY,
            [
                "--policy", str(policy_path), "run",
                "--cwd", str(target), "--session-id", session,
                "--risk", "2", "--", "git", "status", "--short",
            ],
            0,
        )

        write_policy(policy_path, root, state, "1.5.1")
        run(
            LAUNCHER,
            ["--policy", str(policy_path), "--repo", str(repo), "--session-prefix", "old"],
            28,
        )

        write_policy(policy_path, root, state, "1.6.0")
        run(
            LAUNCHER,
            [
                "--policy", str(policy_path), "--repo", str(repo),
                "--session-prefix", "badhead", "--expected-head", "0" * 40,
            ],
            23,
        )

        print(
            "SESSION_LAUNCHER_E2E_OK positive=3 negative=2 "
            "auto_session=valid worktree=isolated base=unchanged"
        )
        return 0
    finally:
        if previous_git_config is None:
            os.environ.pop("GIT_CONFIG_GLOBAL", None)
        else:
            os.environ["GIT_CONFIG_GLOBAL"] = previous_git_config
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

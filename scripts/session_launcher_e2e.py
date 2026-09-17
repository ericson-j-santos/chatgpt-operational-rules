#!/usr/bin/env python3
"""E2E do Session Launcher sobre bootstrap, preflight, sync fast-forward e base suja isolada."""

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
        text=True, capture_output=True, check=False, timeout=90,
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
        capture_output=True, check=True, timeout=30,
    )
    return result.stdout.strip()


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init")
    git(path, "config", "user.email", "launcher-e2e@example.invalid")
    git(path, "config", "user.name", "Launcher E2E")
    (path / "baseline.txt").write_text("baseline\n", encoding="utf-8", newline="\n")
    git(path, "add", "baseline.txt")
    git(path, "commit", "-m", "baseline")
    git(path, "branch", "-M", "main")
    return path


def make_repo(root: Path) -> Path:
    return init_repo(root / "repo")


def make_remote_scenario(root: Path, name: str) -> tuple[Path, str, Path]:
    scenario = root / name
    scenario.mkdir(parents=True)
    local = init_repo(scenario / "local")
    remote = scenario / "remote.git"
    git(scenario, "init", "--bare", str(remote))
    git(local, "remote", "add", "origin", str(remote))
    git(local, "push", "-u", "origin", "main")

    writer = scenario / "writer"
    git(scenario, "clone", str(remote), str(writer))
    git(writer, "config", "user.email", "launcher-e2e@example.invalid")
    git(writer, "config", "user.name", "Launcher E2E")
    git(writer, "checkout", "main")
    (writer / "remote.txt").write_text(f"{name}\n", encoding="utf-8", newline="\n")
    git(writer, "add", "remote.txt")
    git(writer, "commit", "-m", f"{name} remote advance")
    git(writer, "push", "origin", "main")
    return local, git(writer, "rev-parse", "HEAD"), remote


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

        stale, remote_head, _ = make_remote_scenario(root, "sync-positive")
        synced = run(
            LAUNCHER,
            [
                "--policy", str(policy_path),
                "--repo", str(stale),
                "--session-id", "sync-positive",
                "--expected-head", remote_head,
                "--sync-ref", "origin/main",
                "--correlation-id", "launcher-e2e-sync-positive",
            ],
            0,
        )
        synced_payload = json.loads(synced.stdout.splitlines()[-1])
        if synced_payload.get("base_sync") != "fast_forward":
            raise AssertionError("launcher não registrou fast-forward da base")
        if git(stale, "rev-parse", "HEAD") != remote_head:
            raise AssertionError("base stale não avançou exatamente até expected_head")

        dirty, dirty_remote_head, dirty_remote = make_remote_scenario(root, "sync-dirty")
        dirty_original_head = git(dirty, "rev-parse", "HEAD")
        (dirty / "baseline.txt").write_text("dirty-preserved\n", encoding="utf-8", newline="\n")
        dirty_launch = run(
            LAUNCHER,
            [
                "--policy", str(policy_path),
                "--repo", str(dirty),
                "--session-id", "sync-dirty",
                "--expected-head", dirty_remote_head,
                "--sync-ref", "origin/main",
                "--correlation-id", "launcher-e2e-sync-dirty",
            ],
            0,
        )
        dirty_payload = json.loads(dirty_launch.stdout.splitlines()[-1])
        if dirty_payload.get("base_sync") != "isolated_dirty_base":
            raise AssertionError("base suja não foi desviada para base isolada")
        if git(dirty, "rev-parse", "HEAD") != dirty_original_head:
            raise AssertionError("HEAD da base suja original foi alterado")
        if (dirty / "baseline.txt").read_text(encoding="utf-8") != "dirty-preserved\n":
            raise AssertionError("conteúdo rastreado local não foi preservado")
        if not git(dirty, "status", "--porcelain", "--untracked-files=no"):
            raise AssertionError("alteração rastreada original deixou de existir")
        dirty_target = Path(dirty_payload["target_path"])
        if git(dirty_target, "rev-parse", "HEAD") != dirty_remote_head:
            raise AssertionError("worktree isolado não está no SHA remoto esperado")
        if git(dirty_target, "status", "--porcelain"):
            raise AssertionError("worktree isolado não terminou limpo")
        isolated_base = Path(dirty_payload["repo_root"])
        if isolated_base.resolve() == dirty.resolve():
            raise AssertionError("sessão dirty reutilizou indevidamente a base original")
        if git(isolated_base, "remote", "get-url", "origin") != str(dirty_remote):
            raise AssertionError("base isolada não preservou remoto origin")

        divergent, divergent_remote_head, _ = make_remote_scenario(root, "sync-divergent")
        (divergent / "local-only.txt").write_text("local\n", encoding="utf-8", newline="\n")
        git(divergent, "add", "local-only.txt")
        git(divergent, "commit", "-m", "local divergent commit")
        run(
            LAUNCHER,
            [
                "--policy", str(policy_path),
                "--repo", str(divergent),
                "--session-id", "sync-divergent",
                "--expected-head", divergent_remote_head,
                "--sync-ref", "origin/main",
            ],
            23,
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
            "SESSION_LAUNCHER_E2E_OK positive=5 negative=3 "
            "sync=fast_forward dirty=isolated_preserved divergence=blocked "
            "auto_session=valid worktree=isolated"
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

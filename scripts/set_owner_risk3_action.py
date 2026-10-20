#!/usr/bin/env python3
"""Autoriza uma única ação Risk 3 exata, vinculada a repo/HEAD/script limpo."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import owner_risk3_gateway as gateway

CONFIRM = "AUTHORIZE-SPECIFIC-RISK3-ACTION"
MAX_MINUTES = 60
ACTION_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")
SHA40 = re.compile(r"^[0-9a-f]{40}$")


class SpecificActionError(RuntimeError):
    pass


def _git(root: Path, *args: str) -> str:
    cp = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30, check=False,
    )
    if cp.returncode != 0 or cp.stderr.strip():
        raise SpecificActionError("git_validation_failed")
    return cp.stdout.strip()


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix="risk3-specific-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def authorize(
    *,
    config_path: Path,
    action_id: str,
    scope: str,
    environment: str,
    repo_root: Path,
    expected_head: str,
    script: Path,
    args: list[str],
    expires_minutes: int,
    authorization_ref: str,
    confirm: str,
) -> dict[str, Any]:
    if confirm != CONFIRM:
        raise SpecificActionError("confirmation_invalid")
    if not ACTION_RE.fullmatch(action_id):
        raise SpecificActionError("action_id_invalid")
    if environment not in gateway.SAFE_ENVIRONMENTS:
        raise SpecificActionError("environment_not_allowed")
    if not 1 <= expires_minutes <= MAX_MINUTES:
        raise SpecificActionError("expires_minutes_invalid")
    if not isinstance(scope, str) or not scope or len(scope) > 512:
        raise SpecificActionError("scope_invalid")
    if not isinstance(authorization_ref, str) or not 8 <= len(authorization_ref) <= 512:
        raise SpecificActionError("authorization_ref_invalid")
    if SHA40.fullmatch(expected_head) is None:
        raise SpecificActionError("expected_head_invalid")
    if not isinstance(args, list) or len(args) > 64 or not all(
        isinstance(item, str) and 0 < len(item) <= 1024 for item in args
    ):
        raise SpecificActionError("args_invalid")

    root = repo_root.resolve()
    if not root.is_dir():
        raise SpecificActionError("repo_root_missing")
    if Path(_git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise SpecificActionError("repo_root_mismatch")
    if _git(root, "rev-parse", "HEAD") != expected_head:
        raise SpecificActionError("head_mismatch")
    if _git(root, "status", "--porcelain", "--untracked-files=all"):
        raise SpecificActionError("worktree_not_clean")

    script_abs = script if script.is_absolute() else (root / script)
    script_abs = script_abs.resolve()
    try:
        rel = script_abs.relative_to(root)
    except ValueError as exc:
        raise SpecificActionError("script_outside_repo") from exc
    if not script_abs.is_file():
        raise SpecificActionError("script_missing")
    tracked = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--error-unmatch", rel.as_posix()],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=20, check=False,
    )
    if tracked.returncode != 0:
        raise SpecificActionError("script_not_tracked")

    command = [sys.executable, str(script_abs), *args]
    gateway._guard_common(action_id, scope, command, strict_secret_options=True)

    path = config_path.expanduser().resolve()
    config = gateway.load_local_config(path)
    actions = config.get("actions")
    if not isinstance(actions, dict):
        actions = {}
        config["actions"] = actions

    now = datetime.now(timezone.utc)
    action = {
        "environment": environment,
        "scope": scope,
        "expires_at": (now + timedelta(minutes=expires_minutes)).isoformat(),
        "command": command,
        "cwd": str(root),
        "git_head": expected_head,
        "script_sha256": gateway.sha256_file(script_abs),
        "authorization_ref_sha256": hashlib.sha256(
            authorization_ref.encode("utf-8", errors="replace")
        ).hexdigest(),
    }
    gateway.validate_action(action_id, scope, action)

    previous = actions.get(action_id)
    comparable = {k: v for k, v in action.items() if k != "expires_at"}
    previous_comparable = (
        {k: v for k, v in previous.items() if k != "expires_at"}
        if isinstance(previous, dict) else None
    )
    replayed = previous_comparable == comparable and isinstance(previous, dict)
    actions[action_id] = action
    _atomic_write(path, config)
    reread = gateway.load_local_config(path)
    if reread.get("actions", {}).get(action_id) != action:
        raise SpecificActionError("readback_mismatch")

    return {
        "status": "authorized",
        "action_id": action_id,
        "environment": environment,
        "scope_sha256": hashlib.sha256(scope.encode("utf-8")).hexdigest(),
        "cwd_sha256": hashlib.sha256(gateway.normalize_path(root).encode("utf-8")).hexdigest(),
        "git_head": expected_head,
        "script_sha256": action["script_sha256"],
        "expires_at": action["expires_at"],
        "replayed": replayed,
        "broad_development_mode_changed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=gateway.default_config_path())
    parser.add_argument("--action-id", required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--environment", choices=sorted(gateway.SAFE_ENVIRONMENTS), required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--script", type=Path, required=True)
    parser.add_argument("--arg", action="append", default=[])
    parser.add_argument("--expires-minutes", type=int, default=30)
    parser.add_argument("--authorization-ref", required=True)
    parser.add_argument("--confirm", required=True)
    ns = parser.parse_args()
    try:
        result = authorize(
            config_path=ns.config,
            action_id=ns.action_id,
            scope=ns.scope,
            environment=ns.environment,
            repo_root=ns.repo_root,
            expected_head=ns.expected_head,
            script=ns.script,
            args=ns.arg,
            expires_minutes=ns.expires_minutes,
            authorization_ref=ns.authorization_ref,
            confirm=ns.confirm,
        )
    except (OSError, ValueError, SpecificActionError, gateway.Risk3Error) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, sort_keys=True))
        return 20
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

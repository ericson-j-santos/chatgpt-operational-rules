#!/usr/bin/env python3
"""Inicializador único de sessão governada para o Command Gateway."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

import command_gateway as cg
import session_bootstrap as sb
import session_preflight as sp

EXIT_SESSION_LAUNCHER = 28
MIN_RULES_VERSION = (1, 5, 2)
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
HEAD_RE = re.compile(r"^[0-9a-fA-F]{40}$")
SYNC_REF_RE = re.compile(
    r"^(?P<remote>[A-Za-z0-9][A-Za-z0-9._-]*)/(?P<branch>[A-Za-z0-9][A-Za-z0-9._/-]*)$"
)


def parse_version(value: str) -> tuple[int, int, int]:
    match = VERSION_RE.fullmatch(str(value).strip())
    if not match:
        raise cg.GatewayError("rules_version inválida para o session launcher", EXIT_SESSION_LAUNCHER)
    return tuple(int(part) for part in match.groups())


def ensure_min_version(policy: dict) -> str:
    raw = str(policy.get("rules_version", ""))
    if parse_version(raw) < MIN_RULES_VERSION:
        raise cg.GatewayError(
            "Session Launcher exige Command Gateway >= 1.5.2",
            EXIT_SESSION_LAUNCHER,
        )
    return raw


def slug(value: str, limit: int) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-_").lower()
    return (cleaned or "session")[:limit]


def generate_session_id(repo: Path, prefix: str | None = None) -> str:
    base = slug(prefix or repo.name, 16)
    host = slug(socket.gethostname(), 12)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    suffix = f"{os.getpid():x}"
    return sb.validate_session_id(f"{base}-{host}-{stamp}-{suffix}"[:64])


def validate_expected_head(value: str | None) -> str | None:
    if value is None:
        return None
    if not HEAD_RE.fullmatch(value):
        raise cg.GatewayError("expected_head deve ser SHA completo de 40 caracteres", EXIT_SESSION_LAUNCHER)
    return value.lower()


def validate_sync_ref(value: str | None) -> tuple[str, str] | None:
    if value is None:
        return None
    raw = str(value).strip()
    if (
        not raw
        or raw.startswith("-")
        or raw.endswith("/")
        or "//" in raw
        or ".." in raw.split("/")
        or "\\" in raw
        or ":" in raw
        or "@{" in raw
    ):
        raise cg.GatewayError("sync_ref inválida", EXIT_SESSION_LAUNCHER)
    match = SYNC_REF_RE.fullmatch(raw)
    if not match:
        raise cg.GatewayError("sync_ref deve usar o formato remote/branch", EXIT_SESSION_LAUNCHER)
    return match.group("remote"), match.group("branch")


def _tracked_clean(repo: Path) -> None:
    result = cg.run_capture(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=no"],
        repo,
    )
    if result.returncode != 0 or result.stderr.strip():
        raise cg.GatewayError(
            "não foi possível validar alterações rastreadas antes da sincronização",
            cg.EXIT_STATE_CHANGED,
        )
    if result.stdout:
        raise cg.GatewayError(
            "sincronização recusada: base possui alterações rastreadas",
            cg.EXIT_STATE_CHANGED,
        )


def sync_expected_head(
    repo: Path,
    policy: dict,
    expected: str,
    sync_ref: str,
) -> tuple[cg.GitState, bool]:
    current = cg.git_state(repo, True, policy)
    assert current is not None
    if current.head.lower() == expected:
        return current, False

    parsed = validate_sync_ref(sync_ref)
    assert parsed is not None
    remote, branch = parsed
    _tracked_clean(repo)

    fetched = cg.run_capture(
        ["git", "fetch", "--prune", "--no-tags", remote],
        repo,
        timeout=60,
    )
    if fetched.returncode != 0:
        detail = cg.redact((fetched.stderr or fetched.stdout).strip())[-300:]
        raise cg.GatewayError(
            f"sincronização recusada: fetch falhou: {detail}",
            cg.EXIT_STATE_CHANGED,
        )

    remote_ref = f"refs/remotes/{remote}/{branch}^{{commit}}"
    advertised = cg.run_capture(["git", "rev-parse", "--verify", remote_ref], repo)
    if advertised.returncode != 0 or advertised.stderr.strip():
        raise cg.GatewayError(
            "sincronização recusada: referência remota não resolvida",
            cg.EXIT_STATE_CHANGED,
        )
    remote_head = advertised.stdout.strip().lower()
    if remote_head != expected:
        raise cg.GatewayError(
            f"sincronização recusada: {sync_ref}={remote_head} esperado={expected}",
            cg.EXIT_STATE_CHANGED,
        )

    ancestor = cg.run_capture(
        ["git", "merge-base", "--is-ancestor", current.head, expected],
        repo,
    )
    if ancestor.returncode == 1:
        raise cg.GatewayError(
            "sincronização recusada: HEAD local diverge da referência remota",
            cg.EXIT_STATE_CHANGED,
        )
    if ancestor.returncode != 0 or ancestor.stderr.strip():
        raise cg.GatewayError(
            "sincronização recusada: não foi possível provar ancestralidade",
            cg.EXIT_STATE_CHANGED,
        )

    advanced = cg.run_capture(["git", "merge", "--ff-only", expected], repo, timeout=60)
    if advanced.returncode != 0:
        detail = cg.redact((advanced.stderr or advanced.stdout).strip())[-300:]
        raise cg.GatewayError(
            f"sincronização recusada: fast-forward falhou: {detail}",
            cg.EXIT_STATE_CHANGED,
        )

    final = cg.git_state(repo, True, policy)
    assert final is not None
    _tracked_clean(repo)
    if final.head.lower() != expected:
        raise cg.GatewayError(
            f"sincronização incompleta: atual={final.head} esperado={expected}",
            cg.EXIT_STATE_CHANGED,
        )
    return final, True


def launch(
    repo: Path,
    policy_path: Path,
    session_id: str | None,
    session_prefix: str | None,
    correlation_id: str,
    expected_head: str | None,
    sync_ref: str | None = None,
) -> dict:
    policy = cg.load_policy(policy_path)
    rules_version = ensure_min_version(policy)
    cg.assert_allowed_path(repo, policy)
    state = cg.git_state(repo, True, policy)
    assert state is not None
    expected = validate_expected_head(expected_head)
    validated_sync_ref = validate_sync_ref(sync_ref)
    if validated_sync_ref and expected is None:
        raise cg.GatewayError(
            "sync_ref exige expected_head explícito",
            EXIT_SESSION_LAUNCHER,
        )

    base_synced = False
    if expected and state.head.lower() != expected:
        if sync_ref is None:
            raise cg.GatewayError(
                f"HEAD atual diverge do esperado: atual={state.head} esperado={expected}",
                cg.EXIT_STATE_CHANGED,
            )
        state, base_synced = sync_expected_head(repo, policy, expected, sync_ref)

    resolved_session = (
        sb.validate_session_id(session_id)
        if session_id
        else generate_session_id(repo, session_prefix)
    )
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink):
        preflight = sp.preflight(
            repo=repo,
            policy_path=policy_path,
            session_id=resolved_session,
            correlation_id=correlation_id,
            materialize=True,
        )
    target_state = preflight["target_state"]
    result = {
        "timestamp": cg.utc_now(),
        "correlation_id": correlation_id,
        "action": "session_launcher",
        "result": "SESSION_LAUNCH_OK",
        "session_id": resolved_session,
        "repo_root": preflight["repo_root"],
        "target_path": preflight["target_path"],
        "head": target_state["head"],
        "snapshot_sha256": preflight["snapshot_sha256"],
        "rules_version": rules_version,
        "host": preflight["host"],
        "state_validated": True,
        "base_sync": "fast_forward" if base_synced else "not_needed",
        "sync_ref": sync_ref,
    }
    cg.append_event(policy, result)
    cg.emit_json(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Inicializador único de sessão governada")
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--session-id")
    parser.add_argument("--session-prefix")
    parser.add_argument("--correlation-id")
    parser.add_argument("--expected-head")
    parser.add_argument(
        "--sync-ref",
        help="Permite apenas fast-forward até expected-head após fetch da referência remote/branch.",
    )
    ns = parser.parse_args()
    correlation_id = ns.correlation_id or f"launcher-{os.getpid()}"
    try:
        launch(
            repo=ns.repo,
            policy_path=ns.policy,
            session_id=ns.session_id,
            session_prefix=ns.session_prefix,
            correlation_id=correlation_id,
            expected_head=ns.expected_head,
            sync_ref=ns.sync_ref,
        )
        return 0
    except (cg.GatewayError, json.JSONDecodeError, OSError) as exc:
        code = exc.exit_code if isinstance(exc, cg.GatewayError) else EXIT_SESSION_LAUNCHER
        cg.emit_json(
            {
                "timestamp": cg.utc_now(),
                "correlation_id": correlation_id,
                "action": "session_launcher",
                "result": "SESSION_LAUNCH_BLOCKED",
                "gateway_exit_code": code,
                "error": cg.redact(str(exc)),
            },
            file=sys.stderr,
        )
        return code


if __name__ == "__main__":
    raise SystemExit(main())

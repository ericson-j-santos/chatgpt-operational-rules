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


def launch(
    repo: Path,
    policy_path: Path,
    session_id: str | None,
    session_prefix: str | None,
    correlation_id: str,
    expected_head: str | None,
) -> dict:
    policy = cg.load_policy(policy_path)
    rules_version = ensure_min_version(policy)
    cg.assert_allowed_path(repo, policy)
    state = cg.git_state(repo, True, policy)
    assert state is not None
    expected = validate_expected_head(expected_head)
    if expected and state.head.lower() != expected:
        raise cg.GatewayError(
            f"HEAD atual diverge do esperado: atual={state.head} esperado={expected}",
            cg.EXIT_STATE_CHANGED,
        )
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

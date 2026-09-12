#!/usr/bin/env python3
"""Bootstrap obrigatório de sessão para o Command Gateway."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
from pathlib import Path

import command_gateway as cg

EXIT_SESSION_REQUIRED = 25
EXIT_SESSION_CONFLICT = 26
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")


def validate_session_id(session_id: str) -> str:
    if not SESSION_RE.fullmatch(session_id):
        raise cg.GatewayError(
            "session_id inválido; use 3-64 caracteres alfanuméricos, ponto, hífen ou sublinhado",
            EXIT_SESSION_REQUIRED,
        )
    return session_id


def session_file(policy: dict, session_id: str) -> Path:
    directory = cg.state_dir(policy) / "sessions"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{validate_session_id(session_id)}.json"


def worktree_path(policy: dict, session_id: str) -> Path:
    root = Path(cg.expand_path(policy.get("worktree_root", "C:\\dev")))
    prefix = str(policy.get("worktree_prefix", "wt-chat-"))
    if not prefix or any(ch in prefix for ch in '\\/:*?"<>|'):
        raise cg.GatewayError("worktree_prefix inválido", EXIT_SESSION_REQUIRED)
    target = root / f"{prefix}{validate_session_id(session_id)}"
    cg.assert_allowed_path(target, policy)
    return target


def read_reservation(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise cg.GatewayError("reserva de sessão inválida", EXIT_SESSION_REQUIRED) from exc


def write_reservation(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8", newline="\n")


def tracked_tree_clean(repo: Path) -> bool:
    result = cg.run_capture(["git", "status", "--porcelain=v1", "-z", "--untracked-files=no"], repo)
    if result.returncode != 0 or result.stderr.strip():
        raise cg.GatewayError("não foi possível validar alterações rastreadas", cg.EXIT_STATE_CHANGED)
    return not result.stdout


def reserve(repo: Path, policy: dict, session_id: str, correlation_id: str) -> tuple[dict, str]:
    if not policy.get("require_session_bootstrap", False):
        raise cg.GatewayError("política não exige bootstrap de sessão", EXIT_SESSION_REQUIRED)
    cg.assert_allowed_path(repo, policy)
    state = cg.git_state(repo, True, policy)
    assert state is not None
    target = worktree_path(policy, session_id)
    path = session_file(policy, session_id)
    payload = {
        "session_id": session_id,
        "status": "reserved",
        "repo_root": cg.norm(state.repo_root),
        "base_head": state.head,
        "base_branch": state.branch,
        "base_status_digest": state.status_digest,
        "base_status_count": state.status_count,
        "reserved_worktree": cg.norm(target),
        "created_at": cg.utc_now(),
        "host": socket.gethostname(),
        "correlation_id": correlation_id,
    }
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        existing = read_reservation(path)
        if existing.get("repo_root") != payload["repo_root"]:
            raise cg.GatewayError("session_id já reservado para outro repositório", EXIT_SESSION_CONFLICT)
        return existing, "already_reserved"
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
    return payload, "reserved"


def ensure_safe_directory(repo: Path, target: Path, policy: dict, correlation_id: str, session_id: str, expected_head: str) -> None:
    resolved = str(target.resolve()).replace("\\", "/")
    listed = cg.run_capture(["git", "config", "--global", "--get-all", "safe.directory"], repo)
    if listed.returncode not in (0, 1) or listed.stderr.strip():
        raise cg.GatewayError("não foi possível consultar safe.directory", cg.EXIT_STATE_CHANGED)
    existing = {line.strip().replace("\\", "/").casefold() for line in listed.stdout.splitlines() if line.strip()}
    if resolved.casefold() in existing:
        return
    rc = cg.execute(
        cwd=repo, policy=policy,
        args=["git", "config", "--global", "--add", "safe.directory", resolved],
        risk=2, timeout=min(int(policy.get("max_timeout_seconds", 900)), 30),
        expected_head=expected_head, allow_dirty=True, allow_head_change=False,
        correlation_id=correlation_id, session_id=session_id,
    )
    if rc != 0:
        raise cg.GatewayError("não foi possível registrar safe.directory do worktree", rc)


def materialize(reservation: dict, policy: dict, correlation_id: str) -> tuple[dict, str]:
    repo = Path(reservation["repo_root"])
    target = Path(reservation["reserved_worktree"])
    if reservation.get("status") == "materialized":
        ensure_safe_directory(repo, target, policy, correlation_id, reservation["session_id"], reservation["base_head"])
        current = cg.git_state(target, True, policy)
        if current is None or current.head != reservation.get("base_head") or current.status_count:
            raise cg.GatewayError("worktree materializado diverge da reserva", cg.EXIT_STATE_CHANGED)
        return reservation, "already_materialized"
    if target.exists():
        raise cg.GatewayError("caminho reservado já existe sem vínculo válido", EXIT_SESSION_CONFLICT)
    collisions = cg.tracked_case_collisions(repo)
    if collisions:
        details = "; ".join(f"{left} <-> {right}" for left, right in collisions[:5])
        raise cg.GatewayError(f"base possui caminhos rastreados que colidem por casing: {details}", cg.EXIT_STATE_CHANGED)
    if not tracked_tree_clean(repo):
        raise cg.GatewayError("base possui alterações rastreadas; materialização bloqueada", cg.EXIT_STATE_CHANGED)
    rc = cg.execute(
        cwd=repo,
        policy=policy,
        args=["git", "worktree", "add", "--detach", str(target), reservation["base_head"]],
        risk=2,
        timeout=min(int(policy.get("max_timeout_seconds", 900)), 120),
        expected_head=reservation["base_head"],
        allow_dirty=True,
        allow_head_change=False,
        correlation_id=correlation_id,
        session_id=reservation["session_id"],
    )
    if rc != 0:
        raise cg.GatewayError("Command Gateway não materializou o worktree", rc)
    ensure_safe_directory(repo, target, policy, correlation_id, reservation["session_id"], reservation["base_head"])
    created = cg.git_state(target, True, policy)
    if created is None or created.head != reservation["base_head"] or created.status_count:
        raise cg.GatewayError("worktree criado não corresponde à reserva", cg.EXIT_STATE_CHANGED)
    reservation["status"] = "materialized"
    reservation["materialized_at"] = cg.utc_now()
    reservation["worktree_head"] = created.head
    write_reservation(session_file(policy, reservation["session_id"]), reservation)
    return reservation, "materialized"


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap de sessão do Command Gateway")
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--correlation-id")
    parser.add_argument("--materialize", action="store_true")
    ns = parser.parse_args()
    correlation_id = ns.correlation_id or f"bootstrap-{os.getpid()}"
    policy = None
    try:
        policy = cg.load_policy(ns.policy)
        reservation, reserve_result = reserve(ns.repo, policy, ns.session_id, correlation_id)
        materialize_result = "not_requested"
        if ns.materialize:
            reservation, materialize_result = materialize(reservation, policy, correlation_id)
        event = {
            "timestamp": cg.utc_now(),
            "correlation_id": correlation_id,
            "action": "session_bootstrap",
            "session_id": ns.session_id,
            "result": "BOOTSTRAP_OK",
            "reserve_result": reserve_result,
            "materialize_result": materialize_result,
            "repo_root": reservation["repo_root"],
            "base_head": reservation["base_head"],
            "reserved_worktree": reservation["reserved_worktree"],
            "state": reservation["status"],
        }
        cg.append_event(policy, event)
        cg.emit_json(event)
        return 0
    except (cg.GatewayError, json.JSONDecodeError, OSError) as exc:
        code = exc.exit_code if isinstance(exc, cg.GatewayError) else EXIT_SESSION_REQUIRED
        error = {
            "timestamp": cg.utc_now(),
            "correlation_id": correlation_id,
            "action": "session_bootstrap",
            "session_id": ns.session_id,
            "result": "BOOTSTRAP_BLOCKED",
            "gateway_exit_code": code,
            "error": cg.redact(str(exc)),
        }
        if policy is not None:
            try:
                cg.append_event(policy, error)
            except OSError:
                pass
        cg.emit_json(error, file=sys.stderr)
        return code


if __name__ == "__main__":
    raise SystemExit(main())

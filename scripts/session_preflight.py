#!/usr/bin/env python3
"""Captura e valida automaticamente o estado inicial de uma sessão técnica."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
from dataclasses import asdict
from pathlib import Path

import command_gateway as cg
import session_bootstrap as sb

EXIT_PREFLIGHT_FAILED = 27


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_path(policy: dict, session_id: str) -> Path:
    directory = cg.state_dir(policy) / "snapshots"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{sb.validate_session_id(session_id)}.json"


def write_snapshot(path: Path, payload: dict) -> None:
    payload["snapshot_sha256"] = cg.snapshot_sha256(payload)
    temp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temp, path)


def read_valid_snapshot(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise cg.GatewayError("snapshot de preflight inválido", EXIT_PREFLIGHT_FAILED) from exc
    expected = payload.get("snapshot_sha256")
    if not expected or expected != cg.snapshot_sha256(payload):
        raise cg.GatewayError("integridade do snapshot de preflight inválida", EXIT_PREFLIGHT_FAILED)
    return payload


def reservation_matches_base(reservation: dict, state: cg.GitState) -> bool:
    return (
        reservation.get("base_head") == state.head
        and reservation.get("base_branch") == state.branch
        and reservation.get("base_status_digest") == state.status_digest
        and reservation.get("base_status_count") == state.status_count
    )


def preflight(
    repo: Path,
    policy_path: Path,
    session_id: str,
    correlation_id: str,
    materialize: bool,
) -> dict:
    policy = cg.load_policy(policy_path)
    reservation, reserve_result = sb.reserve(repo, policy, session_id, correlation_id)
    materialize_result = "not_requested"
    if materialize:
        reservation, materialize_result = sb.materialize(reservation, policy, correlation_id)

    base_state = cg.git_state(repo, True, policy)
    assert base_state is not None
    base_matches = reservation_matches_base(reservation, base_state)
    if reservation.get("status") == "reserved" and not base_matches:
        raise cg.GatewayError(
            "estado da base divergiu após a reserva; execute novo bootstrap com outra sessão",
            cg.EXIT_STATE_CHANGED,
        )

    target = Path(
        reservation["reserved_worktree"]
        if reservation.get("status") == "materialized"
        else reservation["repo_root"]
    )
    target_before = cg.git_state(target, True, policy)
    assert target_before is not None
    if reservation.get("status") == "materialized":
        if target_before.head != reservation.get("base_head") or target_before.status_count:
            raise cg.GatewayError("worktree reservado não está no estado inicial esperado", cg.EXIT_STATE_CHANGED)

    with cg.repository_lock(target_before, policy, correlation_id):
        locked = cg.git_state(target, True, policy)
        if locked != target_before:
            raise cg.GatewayError("estado mudou durante o preflight", cg.EXIT_STATE_CHANGED)

        payload = {
            "timestamp": cg.utc_now(),
            "correlation_id": correlation_id,
            "session_id": session_id,
            "result": "BOOTSTRAP_OK",
            "state_validated": True,
            "rules_version": str(policy.get("rules_version", "unknown")),
            "host": socket.gethostname(),
            "repo_root": reservation["repo_root"],
            "reserved_worktree": reservation["reserved_worktree"],
            "reservation_status": reservation["status"],
            "reserve_result": reserve_result,
            "materialize_result": materialize_result,
            "base_matches_reservation": base_matches,
            "base_state": asdict(base_state),
            "target_path": cg.norm(target),
            "target_state": asdict(locked),
            "gateway_sha256": sha256_file(Path(cg.__file__).resolve()),
            "policy_sha256": sha256_file(policy_path.resolve()),
        }
        path = snapshot_path(policy, session_id)
        write_snapshot(path, payload)
        persisted = read_valid_snapshot(path)
        if persisted.get("target_state") != asdict(locked):
            raise cg.GatewayError("snapshot persistido diverge do estado capturado", EXIT_PREFLIGHT_FAILED)
        after = cg.git_state(target, True, policy)
        if after != locked:
            raise cg.GatewayError("estado mudou antes da conclusão do preflight", cg.EXIT_STATE_CHANGED)

    event = {
        "timestamp": cg.utc_now(),
        "correlation_id": correlation_id,
        "action": "session_preflight",
        "session_id": session_id,
        "result": "BOOTSTRAP_OK",
        "state_validated": True,
        "repo_root": reservation["repo_root"],
        "target_path": payload["target_path"],
        "base_head": reservation["base_head"],
        "reservation_status": reservation["status"],
        "snapshot_sha256": payload["snapshot_sha256"],
    }
    cg.append_event(policy, event)
    print(json.dumps(payload, ensure_ascii=False))
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Preflight automático de sessão técnica")
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--correlation-id")
    parser.add_argument("--materialize", action="store_true")
    ns = parser.parse_args()
    correlation_id = ns.correlation_id or f"preflight-{os.getpid()}"
    policy = None
    try:
        policy = cg.load_policy(ns.policy)
        preflight(ns.repo, ns.policy, ns.session_id, correlation_id, ns.materialize)
        return 0
    except (cg.GatewayError, json.JSONDecodeError, OSError) as exc:
        code = exc.exit_code if isinstance(exc, cg.GatewayError) else EXIT_PREFLIGHT_FAILED
        error = {
            "timestamp": cg.utc_now(),
            "correlation_id": correlation_id,
            "action": "session_preflight",
            "session_id": ns.session_id,
            "result": "PREFLIGHT_BLOCKED",
            "gateway_exit_code": code,
            "error": cg.redact(str(exc)),
        }
        if policy is not None:
            try:
                cg.append_event(policy, error)
            except OSError:
                pass
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        return code


if __name__ == "__main__":
    raise SystemExit(main())

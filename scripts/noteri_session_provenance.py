"""Read-only proof of the existing Session Launcher and Gateway lock.

A matching launcher log is evidence of a local session, NOT human authorisation.
The environment, cost and fencing gates remain independent and fail closed.
"""
from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts import command_gateway as gateway

MAX_EVENTS_BYTES = 16 * 1024 * 1024
MAX_EVENT_AGE_SECONDS = 900


class ProvenanceBlocked(RuntimeError):
    pass


def _date(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ProvenanceBlocked("session_timestamp_missing")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProvenanceBlocked("session_timestamp_invalid") from exc
    if result.tzinfo is None:
        raise ProvenanceBlocked("session_timestamp_without_timezone")
    return result.astimezone(timezone.utc)


def verify_launcher_and_lock(*, policy: dict[str, Any], reservation: dict[str, Any],
                             snapshot: dict[str, Any], lock_path: Path,
                             cwd: Path, expected_head: str, session_id: str,
                             correlation_id: str, policy_file_sha256: str,
                             now: datetime | None = None) -> dict[str, Any]:
    """Verify stored evidence in Gateway-owned state; do not accept caller flags."""
    host = socket.gethostname()
    if (not isinstance(policy, dict) or not isinstance(reservation, dict)
        or not isinstance(snapshot, dict) or not isinstance(lock_path, Path)
        or not isinstance(cwd, Path)):
        raise ProvenanceBlocked("session_proof_input_invalid")
    if (policy.get("require_session_bootstrap") is not True
        or policy.get("require_preflight_snapshot") is not True
        or snapshot.get("result") != "BOOTSTRAP_OK"
        or snapshot.get("state_validated") is not True
        or snapshot.get("session_id") != session_id
        or snapshot.get("snapshot_sha256") != gateway.snapshot_sha256(snapshot)):
        raise ProvenanceBlocked("session_snapshot_not_verified")
    if (reservation.get("session_id") != session_id
        or reservation.get("status") != "materialized"
        or reservation.get("host") != host
        or snapshot.get("host") != host
        or snapshot.get("reservation_status") != "materialized"
        or snapshot.get("target_state", {}).get("head") != expected_head
        or reservation.get("base_head") != expected_head):
        raise ProvenanceBlocked("session_host_or_head_mismatch")
    if (not isinstance(policy_file_sha256, str)
        or len(policy_file_sha256) != 64
        or snapshot.get("policy_sha256") != policy_file_sha256
        or snapshot.get("gateway_sha256") != gateway.sha256_file(Path(gateway.__file__).resolve())):
        raise ProvenanceBlocked("installed_gateway_policy_not_pinned")
    if (snapshot.get("correlation_id") != reservation.get("correlation_id")
        or snapshot.get("repo_root") != reservation.get("repo_root")
        or gateway.norm(str(cwd)) != gateway.norm(str(snapshot.get("target_path", "")))
        or gateway.norm(str(cwd)) != gateway.norm(str(reservation.get("reserved_worktree", "")))):
        raise ProvenanceBlocked("session_path_or_correlation_mismatch")
    try:
        lock = json.loads(gateway.read_state_text(policy, lock_path))
    except (OSError, ValueError, gateway.GatewayError) as exc:
        raise ProvenanceBlocked("gateway_lock_unreadable") from exc
    if (lock.get("pid") != os.getpid() or lock.get("host") != host
        or lock.get("correlation_id") != correlation_id
        or gateway.norm(str(lock.get("repo_root", ""))) != gateway.norm(str(cwd))):
        raise ProvenanceBlocked("gateway_lock_not_owned")
    try:
        log = gateway.read_state_text(policy, gateway.secure_state_path(policy, "events.jsonl"))
    except (OSError, gateway.GatewayError) as exc:
        raise ProvenanceBlocked("launcher_event_log_unavailable") from exc
    if len(log.encode("utf-8")) > MAX_EVENTS_BYTES:
        raise ProvenanceBlocked("launcher_event_log_too_large")
    timestamp = _date(snapshot.get("timestamp"))
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ProvenanceBlocked("current_time_without_timezone")
    current = current.astimezone(timezone.utc)
    matches = 0
    for line in log.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except (ValueError, TypeError) as exc:
            raise ProvenanceBlocked("launcher_event_log_corrupt") from exc
        if not isinstance(item, dict):
            raise ProvenanceBlocked("launcher_event_log_corrupt")
        if item.get("action") != "session_launcher":
            continue
        if item.get("session_id") != session_id:
            continue
        if (item.get("result") != "SESSION_LAUNCH_OK"
            or item.get("state_validated") is not True
            or item.get("correlation_id") != reservation.get("correlation_id")
            or item.get("snapshot_sha256") != snapshot.get("snapshot_sha256")
            or item.get("host") != host
            or item.get("head") != expected_head
            or gateway.norm(str(item.get("target_path", ""))) != gateway.norm(str(cwd))
            or gateway.norm(str(item.get("repo_root", ""))) != gateway.norm(str(reservation.get("repo_root", "")))):
            raise ProvenanceBlocked("launcher_session_event_mismatch")
        event_time = _date(item.get("timestamp"))
        age = (current - event_time).total_seconds()
        if event_time < timestamp or not 0 <= age <= MAX_EVENT_AGE_SECONDS:
            raise ProvenanceBlocked("launcher_session_event_expired")
        matches += 1
    if matches != 1:
        raise ProvenanceBlocked("launcher_session_evidence_not_unique")
    return {
        "session_launch_ok": True,
        "state_validated": True,
        "exclusive_lease": True,
        "session_snapshot_sha256": snapshot["snapshot_sha256"],
        "policy_sha256": policy_file_sha256,
    }

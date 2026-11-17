"""Monotonic single-host fencing for the fixed Noteri Worker Pool task.

No CLI or auto-install. A reconciled counter must already exist in the private
Gateway state. This is NOT a distributed lease; cross-host takeover remains
blocked until sink-side fencing and state transfer are independently proven.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts import command_gateway as gateway

DIR = "noteri-worker-pool-fencing"
COUNTER = "counter.json"
ACTIVE = "active.claim"
TARGET = r"\Automation\NoteriWorkerPoolApi"
KIND = "noteri-fencing-v1"
MAX_SEQUENCE = 2**63 - 1
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class FencingBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalFence:
    sequence: int
    nonce: str


def _local_host() -> bool:
    return os.name == "nt" and socket.gethostname().casefold() == "noteri"


def _serial(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _counter(sequence: int, host: str) -> dict[str, Any]:
    data = {"kind": KIND, "target": TARGET, "host": host, "sequence": sequence}
    return {**data, "sha256": hashlib.sha256(_serial(data).encode()).hexdigest()}


def _read_counter(policy: dict[str, Any], path: Path, host: str) -> int:
    try:
        data = json.loads(gateway.read_state_text(policy, path))
    except FileNotFoundError as exc:
        raise FencingBlocked("fence_not_provisioned") from exc
    except (OSError, ValueError, gateway.GatewayError) as exc:
        raise FencingBlocked("fence_counter_unreadable") from exc
    if (not isinstance(data, dict) or set(data) !=
            {"kind", "target", "host", "sequence", "sha256"}
        or type(data.get("sequence")) is not int
        or not 0 <= data["sequence"] <= MAX_SEQUENCE
        or data != _counter(data["sequence"], host)):
        raise FencingBlocked("fence_counter_invalid")
    return data["sequence"]


def _owned_lock(policy: dict[str, Any], lock_path: Path, cwd: Path,
                correlation_id: str, host: str) -> None:
    expected = gateway.secure_state_path(policy, "locks", lock_path.name)
    if gateway.lexical_norm(lock_path) != gateway.lexical_norm(expected):
        raise FencingBlocked("fence_invalid_gateway_lock_path")
    try:
        lock = json.loads(gateway.read_state_text(policy, lock_path))
    except (OSError, ValueError, gateway.GatewayError) as exc:
        raise FencingBlocked("fence_gateway_lock_unreadable") from exc
    if (not isinstance(lock, dict) or lock.get("pid") != os.getpid()
        or lock.get("host") != host or lock.get("correlation_id") != correlation_id
        or gateway.norm(str(lock.get("repo_root", ""))) != gateway.norm(str(cwd))):
        raise FencingBlocked("fence_gateway_lock_not_owned")


def issue_local_token(*, policy: dict[str, Any], gateway_lock_path: Path,
                      cwd: Path, correlation_id: str, session_id: str,
                      snapshot_sha256: str) -> LocalFence:
    """Consume a persistent target-wide claim and increase the saved counter.

    Only call after a validated Gateway session and under its exclusive repo
    lock. The target claim intentionally stays present across crashes and
    returns; only a separately authorised reconciliation may remove it.
    Missing/corrupt counter does NOT create a claim or reset the counter.
    """
    if (not isinstance(policy, dict) or not isinstance(gateway_lock_path, Path)
        or not isinstance(cwd, Path) or not isinstance(correlation_id, str)
        or not correlation_id or not isinstance(session_id, str)
        or not gateway.SESSION_ID_RE.fullmatch(session_id)
        or not isinstance(snapshot_sha256, str) or not HEX64.fullmatch(snapshot_sha256)):
        raise FencingBlocked("fence_arguments_unverified")
    if not _local_host():
        raise FencingBlocked("fence_wrong_host")
    host = socket.gethostname()
    _owned_lock(policy, gateway_lock_path, cwd, correlation_id, host)
    gateway.secure_state_directory(policy, DIR)
    active = gateway.secure_state_path(policy, DIR, ACTIVE)
    counter_path = gateway.secure_state_path(policy, DIR, COUNTER)

    # Preflight first: missing state must not poison the active claim.
    if _read_counter(policy, counter_path, host) >= MAX_SEQUENCE:
        raise FencingBlocked("fence_counter_exhausted")
    claim = {
        "kind": KIND, "target": TARGET, "host": host,
        "pid": os.getpid(), "session_id": session_id,
        "correlation_id": correlation_id, "snapshot_sha256": snapshot_sha256,
        "nonce": secrets.token_hex(16),
    }
    try:
        gateway.write_state_text(policy, active, _serial(claim), exclusive=True)
    except FileExistsError as exc:
        raise FencingBlocked("fence_already_claimed") from exc
    except (OSError, gateway.GatewayError) as exc:
        raise FencingBlocked("fence_claim_failed") from exc
    # No cleanup on any exception: partially issued claims fail closed.
    try:
        if json.loads(gateway.read_state_text(policy, active)) != claim:
            raise FencingBlocked("fence_claim_readback_mismatch")
        current = _read_counter(policy, counter_path, host)
        if current >= MAX_SEQUENCE:
            raise FencingBlocked("fence_counter_exhausted")
        next_sequence = current + 1
        gateway.atomic_write_state_text(
            policy, counter_path, _serial(_counter(next_sequence, host))
        )
        if _read_counter(policy, counter_path, host) != next_sequence:
            raise FencingBlocked("fence_counter_readback_mismatch")
        if json.loads(gateway.read_state_text(policy, active)) != claim:
            raise FencingBlocked("fence_claim_changed")
        return LocalFence(next_sequence, claim["nonce"])
    except (OSError, ValueError, gateway.GatewayError) as exc:
        raise FencingBlocked("fence_write_or_readback_failed") from exc


def verify_active_token(*, policy: dict[str, Any], sequence: int, nonce: str,
                        session_id: str, correlation_id: str) -> bool:
    """Sink-side/local readback prerequisite; no second token is issued."""
    if (type(sequence) is not int or sequence < 1
        or not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce)
        or not _local_host()):
        raise FencingBlocked("fence_validation_request_invalid")
    host = socket.gethostname()
    try:
        claim = json.loads(gateway.read_state_text(
            policy, gateway.secure_state_path(policy, DIR, ACTIVE)
        ))
        current = _read_counter(
            policy, gateway.secure_state_path(policy, DIR, COUNTER), host
        )
    except (OSError, ValueError, gateway.GatewayError) as exc:
        raise FencingBlocked("fence_readback_unavailable") from exc
    if (not isinstance(claim, dict) or claim.get("kind") != KIND
        or claim.get("host") != host or claim.get("target") != TARGET
        or claim.get("pid") != os.getpid()
        or claim.get("session_id") != session_id
        or claim.get("correlation_id") != correlation_id
        or claim.get("nonce") != nonce or current != sequence):
        raise FencingBlocked("fence_not_current_owner")
    return True

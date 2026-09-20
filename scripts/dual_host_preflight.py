#!/usr/bin/env python3
"""Valida dois ou mais hosts e escolhe o melhor worker elegível."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REQUIRED_TRUE = (
    "auth_valid",
    "session_launch_ok",
    "state_validated",
    "gateway_ok",
    "controller_semantic_ok",
)

VALID_PROFILES = {"NORMAL", "ESTUDO"}
DEVELOPMENT_WORKLOADS = {"development", "build", "test", "e2e", "agent", "code"}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _reachability(host: dict[str, Any]) -> tuple[bool | None, list[str]]:
    """Separa alcance físico/rede do heartbeat do controller."""
    controller_online = host.get("controller_online") is True
    raw_signals = host.get("reachability_signals")
    positive_signals: list[str] = []
    if isinstance(raw_signals, dict):
        positive_signals = sorted(
            str(name)
            for name, value in raw_signals.items()
            if value is True and str(name).strip()
        )

    explicit = host.get("host_reachable")
    if explicit is True:
        return True, positive_signals
    if explicit is False:
        return False, positive_signals
    if controller_online or positive_signals:
        return True, positive_signals
    return None, positive_signals


def _availability_state(
    *, controller_online: bool, host_reachable: bool | None
) -> tuple[str, str | None]:
    if controller_online:
        return "controller_online", None
    if host_reachable is True:
        return "host_reachable_controller_offline", "restart_controller"
    if host_reachable is False:
        return "host_unreachable", "restore_host_or_network"
    return "controller_offline_host_unknown", "probe_host_independently"


def evaluate_host(
    host: dict[str, Any],
    required_rules_sha: str,
    requested_worktree: str | None,
    requested_workload: str,
) -> dict[str, Any]:
    name = str(host.get("name") or "").strip() or "unknown"
    blockers: list[str] = []
    profile = str(host.get("profile") or "NORMAL").strip().upper()
    if profile not in VALID_PROFILES:
        blockers.append("invalid_profile")
    elif profile == "ESTUDO" and requested_workload in DEVELOPMENT_WORKLOADS:
        blockers.append("profile_estudo")

    controller_online = host.get("controller_online") is True
    host_reachable, reachability_signals = _reachability(host)
    availability_state, recovery_action = _availability_state(
        controller_online=controller_online,
        host_reachable=host_reachable,
    )
    if not controller_online:
        blockers.append("controller_online")

    for field in REQUIRED_TRUE:
        if host.get(field) is not True:
            blockers.append(field)

    rules_sha = str(host.get("rules_sha") or "").lower()
    if rules_sha != required_rules_sha.lower():
        blockers.append("rules_sha_mismatch")

    if _as_int(host.get("status_count"), 1) != 0:
        blockers.append("worktree_dirty")

    reserved = {
        str(item).casefold()
        for item in (host.get("reserved_worktrees") or [])
        if str(item).strip()
    }
    if requested_worktree and requested_worktree.casefold() in reserved:
        blockers.append("worktree_reserved")

    active_tasks = max(0, _as_int(host.get("active_tasks"), 0))
    capacity_score = _as_int(host.get("capacity_score"), 50)
    route_score = capacity_score - (active_tasks * 20)

    return {
        "name": name,
        "eligible": not blockers,
        "blockers": blockers,
        "route_score": route_score,
        "active_tasks": active_tasks,
        "controller_version": str(host.get("controller_version") or "unknown"),
        "controller_online": controller_online,
        "host_reachable": host_reachable,
        "reachability_signals": reachability_signals,
        "availability_state": availability_state,
        "recovery_required": recovery_action is not None,
        "recovery_action": recovery_action,
        "profile": profile,
        "accepts_new_development": profile == "NORMAL",
    }


def evaluate(payload: dict[str, Any]) -> dict[str, Any]:
    required_sha = str(payload.get("required_rules_sha") or "").strip()
    if len(required_sha) != 40:
        raise ValueError("required_rules_sha deve ter SHA completo de 40 caracteres")

    hosts = payload.get("hosts")
    if not isinstance(hosts, list) or len(hosts) < 2:
        raise ValueError("hosts deve conter pelo menos dois hosts")

    requested_workload = str(payload.get("requested_workload") or "development").strip().lower()
    if not requested_workload:
        requested_workload = "development"

    requested_worktree = payload.get("requested_worktree")
    if requested_worktree is not None:
        requested_worktree = str(requested_worktree).strip() or None

    evaluated = [
        evaluate_host(host, required_sha, requested_worktree, requested_workload)
        for host in hosts
        if isinstance(host, dict)
    ]
    if len(evaluated) < 2:
        raise ValueError("pelo menos dois hosts válidos são obrigatórios")

    preferred = str(payload.get("preferred_host") or "")
    for item in evaluated:
        if preferred and item["name"] == preferred:
            item["route_score"] += 5

    eligible = [item for item in evaluated if item["eligible"]]
    eligible.sort(key=lambda item: (-item["route_score"], item["active_tasks"], item["name"]))
    warnings: list[str] = []
    versions = {item["controller_version"] for item in eligible}
    if len(versions) > 1:
        warnings.append("controller_version_mismatch")

    selected = eligible[0]["name"] if eligible else None
    secondary = eligible[1]["name"] if len(eligible) > 1 else None

    return {
        "ready": bool(eligible),
        "selected_host": selected,
        "secondary_host": secondary,
        "eligible_hosts": [item["name"] for item in eligible],
        "blocked_hosts": {
            item["name"]: item["blockers"]
            for item in evaluated
            if not item["eligible"]
        },
        "warnings": warnings,
        "required_rules_sha": required_sha.lower(),
        "requested_worktree": requested_worktree,
        "requested_workload": requested_workload,
        "correlation_id": payload.get("correlation_id"),
        "evaluated_hosts": evaluated,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Preflight e roteamento governado entre hosts")
    parser.add_argument("--input", type=Path, required=True, help="JSON com evidências dos hosts")
    parser.add_argument("--output", type=Path, help="Arquivo JSON opcional para evidência")
    args = parser.parse_args()

    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        result = evaluate(payload)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(json.dumps({"ready": False, "error": str(exc)}, ensure_ascii=False))
        return 2

    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True)
    print(rendered)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8", newline="\n")
    return 0 if result["ready"] else 3


if __name__ == "__main__":
    raise SystemExit(main())

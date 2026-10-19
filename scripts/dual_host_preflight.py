#!/usr/bin/env python3
"""Valida dois ou mais hosts e escolhe o melhor worker elegível."""

from __future__ import annotations

import argparse
import json
import re
import os
import tempfile
from datetime import datetime, timezone
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
DEVELOPMENT_WORKLOADS = {"development", "build", "test", "e2e", "agent", "code", "hml", "homologation"}


# These inputs are evidence from a trusted producer, not credentials or permission.
# The selector is a pure decision function: it never fetches, dispatches or repairs.
MAX_TRANSPORT_EVIDENCE_AGE_SECONDS = 300
QUALIFICATION_STEP = "Verify governed host qualification"


def _sha(value: Any, length: int = 40) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{" + str(length) + r"}", value) is not None


def _fresh(value: Any, now: datetime) -> bool:
    if not isinstance(value, str):
        return False
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            return False
        age = (now - observed).total_seconds()
        return 0 <= age <= MAX_TRANSPORT_EVIDENCE_AGE_SECONDS
    except (ValueError, TypeError, OverflowError):
        return False


def _github_transport_blocker(
    host: dict[str, Any], rules_sha: str, correlation_id: Any, now: datetime,
) -> str | None:
    """Validate bound API job + independently verified Gateway receipt.

    This does NOT authenticate self-reported JSON. The trusted producer must read
    the GitHub job and actual artifact independently and verify their integrity.
    Ready means candidate eligibility only; the executor must revalidate its lease
    and live session before use. It is never SQL HML certification.
    """
    proof = host.get("transport_evidence")
    if not isinstance(proof, dict):
        return "github_actions_evidence_missing"
    job = proof.get("job")
    if not isinstance(job, dict):
        return "github_actions_job_missing"
    for field in ("id", "run_id", "runner_id", "run_attempt"):
        if type(job.get(field)) is not int or job[field] <= 0:
            return "github_actions_no_pickup"
    if job.get("status") != "completed" or job.get("conclusion") != "success":
        return "github_actions_job_not_successful"
    steps = job.get("steps")
    if not isinstance(steps, list) or not any(
        isinstance(step, dict) and step.get("name") == QUALIFICATION_STEP
        and step.get("status") == "completed" and step.get("conclusion") == "success"
        for step in steps
    ):
        return "github_actions_qualification_not_executed"
    if proof.get("schema_version") != "1" or proof.get("source") != "github_api_and_gateway_receipt":
        return "github_actions_evidence_source_invalid"
    if proof.get("independent_readback_verified") is not True:
        return "github_actions_readback_unverified"
    if not all(_sha(proof.get(field), 64) for field in ("artifact_sha256", "readback_sha256")):
        return "github_actions_integrity_missing"
    if not _fresh(proof.get("observed_at"), now) or not _fresh(job.get("completed_at"), now):
        return "github_actions_evidence_not_fresh"
    repository = host.get("execution_repository")
    source_sha = host.get("expected_source_sha")
    runner_name = host.get("runner_name")
    if (not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}", repository)
            or not _sha(source_sha) or not isinstance(runner_name, str) or not runner_name.strip()):
        return "github_actions_expected_target_missing"
    if (proof.get("repository") != repository or job.get("head_sha") != source_sha
            or job.get("runner_name") != runner_name
            or job.get("html_url") != f"https://github.com/{repository}/actions/runs/{job['run_id']}/job/{job['id']}"):
        return "github_actions_target_mismatch"
    labels = job.get("labels")
    if not isinstance(labels, list) or "self-hosted" not in labels:
        return "github_actions_not_self_hosted"
    receipt = proof.get("receipt")
    if not isinstance(receipt, dict):
        return "github_actions_receipt_missing"
    if (not isinstance(correlation_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", correlation_id)):
        return "github_actions_correlation_missing"
    expected = {
        "host": host.get("name"), "repository": repository,
        "source_sha": source_sha, "rules_sha": rules_sha,
        "run_id": job["run_id"], "job_id": job["id"], "runner_id": job["runner_id"],
        "run_attempt": job["run_attempt"], "runner_name": runner_name,
        "correlation_id": correlation_id, "profile": host.get("profile"),
    }
    if any(type(receipt.get(key)) is not type(value) or receipt.get(key) != value
           for key, value in expected.items()):
        return "github_actions_receipt_binding_mismatch"
    if not _fresh(receipt.get("generated_at"), now):
        return "github_actions_receipt_not_fresh"
    if receipt.get("session_result") != "SESSION_LAUNCH_OK":
        return "github_actions_session_not_launched"
    if any(receipt.get(key) is not True for key in (
        "state_validated", "gateway_ok", "auth_valid", "independent_effect_verified"
    )):
        return "github_actions_session_unverified"
    session_id = receipt.get("session_id")
    if (not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", session_id)
            or not _sha(receipt.get("snapshot_sha256"), 64)):
        return "github_actions_session_snapshot_missing"
    if host.get("host_reachable") is False:
        return "github_actions_reachability_conflict"
    if not isinstance(host.get("profile"), str) or host["profile"] not in VALID_PROFILES:
        return "github_actions_profile_unverified"
    return None


def _capability_requirements(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 32:
        raise ValueError("required_capabilities inválido")
    for key, expected in value.items():
        if (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", key)
                or not isinstance(expected, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", expected)):
            raise ValueError("required_capabilities inválido")
    return value


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
    *, correlation_id: Any = None,
    required_capabilities: dict[str, str] | None = None,
    now: datetime | None = None,
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
    transport = host.get("transport", "rdc")
    transport_valid = False
    if transport == "rdc":
        if not controller_online:
            blockers.append("controller_online")
        if host.get("controller_semantic_ok") is not True:
            blockers.append("controller_semantic_ok")
        transport_valid = controller_online and host.get("controller_semantic_ok") is True
    elif transport == "github_actions":
        reason = _github_transport_blocker(
            host, required_rules_sha, correlation_id, now or datetime.now(timezone.utc)
        )
        if reason:
            blockers.append(reason)
        else:
            transport_valid = True
            host_reachable = True
            availability_state = "github_actions_verified"
            recovery_action = None
            reachability_signals = sorted(set(reachability_signals) | {"github_actions_pickup"})
    else:
        blockers.append("transport_not_allowed")
    if transport != "rdc" and not transport_valid:
        # Missing controller must never automatically request an RDC recovery.
        recovery_action = "qualify_selected_transport"
    for field in REQUIRED_TRUE:
        if field != "controller_semantic_ok" and host.get(field) is not True:
            blockers.append(field)

    requirements = _capability_requirements(required_capabilities)
    available = host.get("capabilities", {})
    if transport == "github_actions":
        proof = host.get("transport_evidence", {})
        receipt = proof.get("receipt", {}) if isinstance(proof, dict) else {}
        available = receipt.get("capabilities", {}) if isinstance(receipt, dict) else {}
    if not isinstance(available, dict):
        available = {}
    for capability, expected in requirements.items():
        if available.get(capability) != expected:
            blockers.append("capability_missing:" + capability)
    if transport == "github_actions" and type(host.get("status_count")) is not int:
        blockers.append("worktree_state_unverified")

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
        "transport": transport if isinstance(transport, str) else "invalid",
        "transport_valid": transport_valid,
        "host_reachable": host_reachable,
        "reachability_signals": reachability_signals,
        "availability_state": availability_state,
        "recovery_required": recovery_action is not None,
        "recovery_action": recovery_action,
        "profile": profile,
        "accepts_new_development": profile == "NORMAL",
    }


def evaluate(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("payload deve conter um objeto")
    required_sha = str(payload.get("required_rules_sha") or "").strip()
    if not _sha(required_sha.lower()):
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

    requirements = _capability_requirements(payload.get("required_capabilities"))
    now = datetime.now(timezone.utc)
    names = [str(host.get("name", "")).strip().casefold() for host in hosts if isinstance(host, dict)]
    if len(names) != len(set(names)) or not all(names):
        raise ValueError("hosts devem possuir nomes únicos e não vazios")
    evaluated = [
        evaluate_host(
            host, required_sha.lower(), requested_worktree, requested_workload,
            correlation_id=payload.get("correlation_id"),
            required_capabilities=requirements, now=now,
        )
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
        "decision_only": True,
        "hml_verified": False,
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



def _write_evidence(path: Path, rendered: str) -> None:
    """Atomically replace the decision; never follow an existing output symlink."""
    if path.is_symlink():
        raise OSError("evidence_symlink_not_allowed")
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description="Preflight e roteamento governado entre hosts")
    parser.add_argument("--input", type=Path, required=True, help="JSON com evidências dos hosts")
    parser.add_argument("--output", type=Path, help="Arquivo JSON opcional para evidência")
    args = parser.parse_args()

    code = 0
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        result = evaluate(payload)
        code = 0 if result["ready"] else 3
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError, OverflowError):
        result = {"ready": False, "error": "input_invalid_or_unavailable",
                  "decision_only": True, "hml_verified": False}
        code = 2

    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True)
    if args.output:
        try:
            _write_evidence(args.output, rendered)
        except (OSError, UnicodeError):
            print(json.dumps({"ready": False, "error": "evidence_write_failed",
                              "decision_only": True, "hml_verified": False}))
            return 2
    print(rendered)
    return code


if __name__ == "__main__":
    raise SystemExit(main())

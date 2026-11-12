#!/usr/bin/env python3
"""Preflight estrito (somente decisão) do par físico DEV ReqSys.

IMPORTANTE: JSON não é atestado. O produtor deve autenticar a API GitHub,
validar os bytes dos recibos e o Gateway independentemente. Saídas deste
verificador não autorizam dispatch/merge/deploy e não provam E2E físico.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROLES = {"builder": "DESKTOP-PDQK954", "validator": "DESKTOP-RP23OGS"}
MAX_AGE_SECONDS = 300
SHA40 = re.compile(r"[0-9a-f]{40}\Z")
SHA64 = re.compile(r"[0-9a-f]{64}\Z")
SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]{8,128}\Z")


def _sha(value: Any, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and bool(pattern.fullmatch(value))


def _fresh(value: Any, now: datetime) -> bool:
    if not isinstance(value, str):
        return False
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return False
        return 0 <= (now - dt).total_seconds() <= MAX_AGE_SECONDS
    except (ValueError, OverflowError):
        return False


def _positive_integer(value: Any) -> bool:
    return type(value) is int and value > 0


def evaluate(payload: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Verifica consistência do pacote vindo de um produtor autenticado.

    Não consulta API, não autentica o produtor, não reserva lease e não despacha.
    Portanto, jamais devolve ready_for_work=True.
    """
    if not isinstance(payload, dict):
        raise ValueError("entrada precisa ser um objeto")
    now = now or datetime.now(timezone.utc)
    blockers: list[str] = []
    rules_sha = payload.get("required_rules_sha")
    correlation = payload.get("correlation_id")
    if not _sha(rules_sha, SHA40):
        blockers.append("rules_sha_invalid")
    if not isinstance(correlation, str) or not SAFE_ID.fullmatch(correlation):
        blockers.append("correlation_invalid")
    if payload.get("environment") != "DEV":
        blockers.append("environment_not_dev")
    if (payload.get("cost_state") != "COST_ZERO_CONFIRMED"
            or payload.get("independent_cost_verification") is not True):
        blockers.append("cost_unverified")

    hosts = payload.get("hosts")
    if not isinstance(hosts, list) or len(hosts) != 2 or any(not isinstance(x, dict) for x in hosts):
        hosts = []
        blockers.append("exactly_two_host_records_required")
    by_name: dict[str, dict[str, Any]] = {}
    for host in hosts:
        name = host.get("name")
        if not isinstance(name, str) or name in by_name:
            blockers.append("duplicate_or_invalid_host_name")
        else:
            by_name[name] = host
    if set(by_name) != set(ROLES.values()):
        blockers.append("physical_pair_mismatch")

    physical_ids: list[str] = []
    runner_ids: list[int] = []
    for role, name in ROLES.items():
        host = by_name.get(name)
        if host is None:
            blockers.append(f"{role}:host_missing")
            continue
        if host.get("role") != role:
            blockers.append(f"{role}:role_mismatch")
        if host.get("transport") != "github_actions":
            blockers.append(f"{role}:physical_transport_not_proven")
        if host.get("profile") != "NORMAL":
            blockers.append(f"{role}:profile_not_normal")
        if host.get("rules_sha") != rules_sha:
            blockers.append(f"{role}:rules_sha_mismatch")
        if type(host.get("status_count")) is not int or host["status_count"] != 0:
            blockers.append(f"{role}:worktree_not_clean")
        if any(host.get(flag) is not True for flag in
               ("auth_valid", "session_launch_ok", "state_validated", "gateway_ok")):
            blockers.append(f"{role}:session_or_gateway_not_verified")
        proof = host.get("physical_proof")
        if not isinstance(proof, dict):
            blockers.append(f"{role}:physical_receipt_missing")
            continue
        if (proof.get("source") != "github_api_and_gateway_receipt"
                or proof.get("independent_readback_verified") is not True
                or proof.get("artifact_bytes_verified") is not True):
            blockers.append(f"{role}:independent_proof_missing")
        if not _fresh(proof.get("observed_at"), now) or not _fresh(proof.get("job_completed_at"), now):
            blockers.append(f"{role}:physical_proof_stale")
        for fld in ("artifact_sha256", "readback_sha256", "physical_identity_sha256"):
            if not _sha(proof.get(fld), SHA64):
                blockers.append(f"{role}:{fld}_invalid")
        if _sha(proof.get("physical_identity_sha256"), SHA64):
            physical_ids.append(proof["physical_identity_sha256"])
        if not _positive_integer(proof.get("runner_id")):
            blockers.append(f"{role}:runner_id_invalid")
        else:
            runner_ids.append(proof["runner_id"])
        if not all(_positive_integer(proof.get(key)) for key in ("job_id", "run_id", "run_attempt")):
            blockers.append(f"{role}:job_identity_invalid")
        if (proof.get("runner_name") != host.get("runner_name")
                or proof.get("host") != name
                or proof.get("role") != role
                or proof.get("rules_sha") != rules_sha
                or proof.get("correlation_id") != correlation
                or not _sha(proof.get("source_sha"), SHA40)
                or proof.get("job_head_sha") != proof.get("source_sha")):
            blockers.append(f"{role}:physical_binding_mismatch")
        if (proof.get("qualification_step") != "completed/success"
                or proof.get("job_conclusion") != "success"
                or proof.get("job_status") != "completed"):
            blockers.append(f"{role}:physical_qualification_not_passed")
        if proof.get("isolated_workspace_verified") is not True:
            blockers.append(f"{role}:workspace_isolation_unverified")
        if role == "validator" and proof.get("validation_read_only") is not True:
            blockers.append("validator:write_scope_not_blocked")
    if len(physical_ids) == 2 and physical_ids[0] == physical_ids[1]:
        blockers.append("same_physical_machine")
    if len(runner_ids) == 2 and runner_ids[0] == runner_ids[1]:
        blockers.append("same_runner")

    mode = payload.get("mode")
    if mode not in ("parallel_independent", "validation_handoff"):
        blockers.append("execution_mode_invalid")

    assignments = payload.get("assignments")
    if not isinstance(assignments, list) or len(assignments) != 2 or any(not isinstance(a, dict) for a in assignments):
        assignments = []
        blockers.append("exactly_two_task_assignments_required")
    tasks: list[str] = []
    branches: list[str] = []
    worktrees: list[str] = []
    lease_digests: list[str] = []
    for role, name in ROLES.items():
        matches = [a for a in assignments if a.get("role") == role]
        if len(matches) != 1:
            blockers.append(f"{role}:assignment_missing_or_duplicate")
            continue
        assignment = matches[0]
        if assignment.get("host") != name:
            blockers.append(f"{role}:assignment_host_mismatch")
        for field, collection in (("task_id", tasks), ("branch", branches), ("worktree", worktrees)):
            value = assignment.get(field)
            if not isinstance(value, str) or not value.strip():
                blockers.append(f"{role}:{field}_missing")
            else:
                collection.append(value.casefold())
        if not _sha(assignment.get("lease_token_digest"), SHA64):
            blockers.append(f"{role}:lease_digest_missing")
        else:
            lease_digests.append(assignment["lease_token_digest"])
        if not _positive_integer(assignment.get("lease_generation")):
            blockers.append(f"{role}:lease_generation_missing")
        if assignment.get("exclusive_lease_verified") is not True:
            blockers.append(f"{role}:exclusive_lease_unverified")
        if role == "validator" and assignment.get("read_only") is not True:
            blockers.append("validator:assignment_not_read_only")
        if not _sha(assignment.get("task_source_sha"), SHA40):
            blockers.append(f"{role}:task_sha_invalid")
    for collection, label in ((tasks, "task"), (branches, "branch"),
                              (worktrees, "worktree"), (lease_digests, "lease")):
        if len(collection) == 2 and collection[0] == collection[1]:
            if label == "task" and mode == "validation_handoff":
                continue  # A mesma tarefa é permitida somente com transferência cercada.
            blockers.append(f"duplicate_{label}_ownership")

    if mode == "validation_handoff":
        if len(tasks) != 2 or tasks[0] != tasks[1]:
            blockers.append("handoff_task_mismatch")
        by_role = {a["role"]: a for a in assignments if isinstance(a.get("role"), str) and a["role"] in ROLES}
        handoff = payload.get("handoff")
        if not isinstance(handoff, dict):
            blockers.append("handoff_evidence_missing")
        elif set(by_role) != set(ROLES):
            blockers.append("handoff_assignments_missing")
        else:
            builder, validator = by_role["builder"], by_role["validator"]
            produced = handoff.get("produced_sha")
            if (not _sha(produced, SHA40)
                    or builder.get("produced_sha") != produced
                    or validator.get("task_source_sha") != produced
                    or builder.get("task_source_sha") == produced):
                blockers.append("handoff_sha_binding_invalid")
            if (handoff.get("task_id") != builder.get("task_id")
                    or handoff.get("task_id") != validator.get("task_id")
                    or handoff.get("correlation_id") != correlation):
                blockers.append("handoff_identity_mismatch")
            bgen = builder.get("lease_generation")
            vgen = validator.get("lease_generation")
            if (not _positive_integer(bgen) or not _positive_integer(vgen)
                    or vgen <= bgen
                    or handoff.get("released_builder_generation") != bgen
                    or handoff.get("claimed_validator_generation") != vgen
                    or handoff.get("builder_lease_released_verified") is not True
                    or handoff.get("validator_lease_acquired_verified") is not True):
                blockers.append("handoff_lease_fencing_unverified")
            digest = handoff.get("checkpoint_sha256")
            if (not _sha(digest, SHA64)
                    or handoff.get("independent_readback_sha256") != digest
                    or handoff.get("checkpoint_ref") != f"cas:sha256:{digest}"
                    or handoff.get("checkpoint_source_sha") != produced
                    or handoff.get("independent_bytes_verified") is not True
                    or handoff.get("checkpoint_external_to_hosts") is not True):
                blockers.append("handoff_durable_checkpoint_unverified")

    return {
        "pair_candidate_consistent": len(blockers) == 0,
        "blockers": sorted(set(blockers)),
        "roles": ROLES,
        "decision_only": True,
        "ready_for_work": False,
        "physical_simultaneity_verified": False,
        "cross_host_replay_verified": False,
        "dispatch_authorized": False,
        "requires_authenticated_producer_and_external_e2e": True,
        "correlation_id": correlation,
        "required_rules_sha": rules_sha,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Preflight conservador do par físico ReqSys")
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    try:
        data = json.loads(args.input.read_text(encoding="utf-8"))
        result = evaluate(data)
    except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        result = {"pair_candidate_consistent": False, "blockers": ["input_invalid"],
                  "decision_only": True, "ready_for_work": False,
                  "dispatch_authorized": False}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["pair_candidate_consistent"] else 3


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Planeja e executa remediações governadas do Repository Governance Control Plane.

Regras:
- nunca faz merge direto;
- update_branch só ocorre com PR aberto, mesmo head SHA e sem conflito;
- dispatch da Governed Merge Queue só ocorre com PR não-draft, atualizado e
  todos os workflows obrigatórios verdes no HEAD atual;
- toda mutação exige modo apply explícito;
- o executor não lê nem persiste credenciais: a autenticação é do cliente gh.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "config" / "repository-governance-control-plane.json"
DEFAULT_EVIDENCE = ROOT / "evidence" / "repository-governance-control-plane.json"
DEFAULT_PLAN = ROOT / "evidence" / "repository-governance-remediation-plan.json"
DEFAULT_RESULT = ROOT / "evidence" / "repository-governance-remediation-result.json"
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from repository_governance_control_plane import (  # noqa: E402
    GovernanceControlPlaneError,
    latest_required_workflow_states,
)


class RemediationError(RuntimeError):
    pass


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RemediationError(f"JSON inválido em {path}: {exc}") from None


def canonical_action_id(payload: dict[str, Any]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def policy_by_repo(policy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item["repository"]: item
        for item in policy.get("repositories", [])
        if isinstance(item, dict) and isinstance(item.get("repository"), str)
    }


def _action(base: dict[str, Any]) -> dict[str, Any]:
    payload = dict(base)
    payload["action_id"] = canonical_action_id(base)
    return payload


def plan_remediations(
    policy: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    policies = policy_by_repo(policy)
    actions: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []

    for repository in evidence.get("repositories", []):
        repo_name = repository.get("repository")
        repo_policy = policies.get(repo_name)
        if not repo_policy or repo_policy.get("mode") != "enforce":
            continue

        remediation = repo_policy.get("remediation") or {}
        direct_merge = bool(remediation.get("direct_merge"))
        if direct_merge:
            blockers.append(
                {
                    "repository": repo_name,
                    "kind": "unsafe_policy",
                    "reason": "direct_merge_must_remain_false",
                }
            )

        if "branch_unprotected" in repository.get("violations", []):
            strategy = remediation.get("branch_protection", "report_only")
            blockers.append(
                {
                    "repository": repo_name,
                    "kind": "branch_protection_drift",
                    "strategy": strategy,
                    "reason": (
                        "provider_capability_unavailable"
                        if strategy == "provider_capability_blocked"
                        else "admin_remediation_not_enabled"
                    ),
                }
            )

        for pull in repository.get("open_pull_requests", []):
            number = pull.get("number")
            head_sha = pull.get("head_sha")
            reasons = set(pull.get("reasons") or [])
            if not isinstance(number, int) or not isinstance(head_sha, str):
                continue

            if (
                remediation.get("update_branch") is True
                and "behind_base" in reasons
                and "conflict" not in reasons
            ):
                actions.append(
                    _action(
                        {
                            "type": "update_branch",
                            "repository": repo_name,
                            "pr_number": number,
                            "expected_head_sha": head_sha,
                            "base_ref": pull.get("base_ref") or repo_policy.get("default_branch"),
                            "reason": "behind_base",
                        }
                    )
                )

            queue = remediation.get("merge_queue") or {}
            if pull.get("decision") == "allow" and queue.get("enabled") is True:
                actions.append(
                    _action(
                        {
                            "type": "dispatch_merge_queue",
                            "repository": repo_name,
                            "pr_number": number,
                            "expected_head_sha": head_sha,
                            "workflow": queue.get("workflow"),
                            "ref": queue.get("ref") or repo_policy.get("default_branch"),
                            "pr_input": queue.get("pr_input") or "pr_number",
                            "reason": "ready_for_governed_queue",
                        }
                    )
                )

    actions.sort(
        key=lambda item: (
            str(item.get("repository")),
            int(item.get("pr_number") or 0),
            str(item.get("type")),
        )
    )
    blockers.sort(
        key=lambda item: (
            str(item.get("repository")),
            str(item.get("kind")),
        )
    )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_correlation_id": evidence.get("correlation_id"),
        "source_policy_sha256": evidence.get("policy_sha256"),
        "actions": actions,
        "blockers": blockers,
        "summary": {
            "actions_total": len(actions),
            "update_branch": sum(1 for item in actions if item["type"] == "update_branch"),
            "dispatch_merge_queue": sum(
                1 for item in actions if item["type"] == "dispatch_merge_queue"
            ),
            "blockers_total": len(blockers),
        },
    }


def run_gh(
    *args: str,
    input_payload: dict[str, Any] | None = None,
) -> Any:
    command = ["gh", "api", *args]
    completed = subprocess.run(
        command,
        input=(json.dumps(input_payload) if input_payload is not None else None),
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "gh api failed"
        raise RemediationError(message[:800])
    if not completed.stdout.strip():
        return None
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RemediationError(f"gh api retornou JSON inválido: {exc}") from None


def _pull(repository: str, number: int) -> dict[str, Any]:
    payload = run_gh(f"repos/{repository}/pulls/{number}")
    if not isinstance(payload, dict):
        raise RemediationError("PR não encontrada")
    return payload


def _compare(repository: str, base_ref: str, head_sha: str) -> dict[str, Any]:
    payload = run_gh(f"repos/{repository}/compare/{base_ref}...{head_sha}")
    if not isinstance(payload, dict):
        raise RemediationError("comparação de commits inválida")
    return payload


def _workflow_runs(repository: str, head_sha: str) -> list[dict[str, Any]]:
    payload = run_gh(
        f"repos/{repository}/actions/runs?head_sha={head_sha}&event=pull_request&per_page=100"
    )
    runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
    if not isinstance(runs, list):
        raise RemediationError("lista de workflows inválida")
    return runs


def _validate_action_policy(
    action: dict[str, Any],
    repo_policy: dict[str, Any],
) -> None:
    remediation = repo_policy.get("remediation") or {}
    action_type = action.get("type")
    if repo_policy.get("mode") != "enforce":
        raise RemediationError("repositório não está em modo enforce")
    if remediation.get("direct_merge"):
        raise RemediationError("política insegura: direct_merge deve ser false")
    if action_type == "update_branch" and remediation.get("update_branch") is not True:
        raise RemediationError("update_branch não autorizado pela política")
    if action_type == "dispatch_merge_queue":
        queue = remediation.get("merge_queue") or {}
        if queue.get("enabled") is not True:
            raise RemediationError("merge queue não habilitada pela política")
        if action.get("workflow") != queue.get("workflow"):
            raise RemediationError("workflow divergente da política")
    if action_type not in {"update_branch", "dispatch_merge_queue"}:
        raise RemediationError(f"tipo de ação não suportado: {action_type}")


def apply_update_branch(
    action: dict[str, Any],
    repo_policy: dict[str, Any],
) -> dict[str, Any]:
    repository = action["repository"]
    number = int(action["pr_number"])
    expected_head = action["expected_head_sha"]
    pull = _pull(repository, number)

    if pull.get("state") != "open":
        return {"result": "STALE_NOOP", "reason": "pr_not_open"}
    observed_head = ((pull.get("head") or {}).get("sha") or "")
    if observed_head != expected_head:
        return {
            "result": "STALE_NOOP",
            "reason": "head_sha_changed",
            "observed_head_sha": observed_head,
        }
    if pull.get("mergeable") is False or pull.get("mergeable_state") == "dirty":
        return {"result": "BLOCKED", "reason": "conflict"}

    base_ref = action.get("base_ref") or repo_policy.get("default_branch")
    comparison = _compare(repository, base_ref, expected_head)
    if int(comparison.get("behind_by", 0)) <= 0:
        return {"result": "NOOP", "reason": "already_up_to_date"}

    run_gh(
        "--method",
        "PUT",
        f"repos/{repository}/pulls/{number}/update-branch",
        "--input",
        "-",
        input_payload={"expected_head_sha": expected_head},
    )

    for attempt in range(8):
        current = _pull(repository, number)
        current_head = ((current.get("head") or {}).get("sha") or "")
        if current_head and current_head != expected_head:
            check = _compare(repository, base_ref, current_head)
            if int(check.get("behind_by", 0)) == 0:
                return {
                    "result": "UPDATED",
                    "previous_head_sha": expected_head,
                    "current_head_sha": current_head,
                    "behind_by": 0,
                }
        if attempt < 7:
            time.sleep(min(2 ** attempt, 8))

    raise RemediationError("update_branch não confirmado por leitura independente")


def apply_dispatch_merge_queue(
    action: dict[str, Any],
    repo_policy: dict[str, Any],
) -> dict[str, Any]:
    repository = action["repository"]
    number = int(action["pr_number"])
    expected_head = action["expected_head_sha"]
    pull = _pull(repository, number)

    if pull.get("state") != "open":
        return {"result": "STALE_NOOP", "reason": "pr_not_open"}
    if pull.get("draft"):
        return {"result": "BLOCKED", "reason": "draft"}
    observed_head = ((pull.get("head") or {}).get("sha") or "")
    if observed_head != expected_head:
        return {
            "result": "STALE_NOOP",
            "reason": "head_sha_changed",
            "observed_head_sha": observed_head,
        }
    if pull.get("mergeable") is not True or pull.get("mergeable_state") == "dirty":
        return {"result": "BLOCKED", "reason": "not_mergeable"}

    base_ref = (pull.get("base") or {}).get("ref") or repo_policy.get("default_branch")
    comparison = _compare(repository, base_ref, expected_head)
    if int(comparison.get("behind_by", 0)) != 0:
        return {"result": "BLOCKED", "reason": "behind_base"}

    states = latest_required_workflow_states(
        _workflow_runs(repository, expected_head),
        repo_policy.get("required_workflows", []),
        head_sha=expected_head,
    )
    if any(item["state"] != "success" for item in states):
        return {
            "result": "BLOCKED",
            "reason": "required_gate_not_green",
            "required_workflows": states,
        }

    workflow = action["workflow"]
    ref = action["ref"]
    pr_input = action["pr_input"]
    before_payload = run_gh(
        f"repos/{repository}/actions/workflows/{workflow}/runs?event=workflow_dispatch&per_page=20"
    )
    before_ids = {
        item.get("id")
        for item in (before_payload or {}).get("workflow_runs", [])
        if isinstance(item, dict)
    }

    run_gh(
        "--method",
        "POST",
        f"repos/{repository}/actions/workflows/{workflow}/dispatches",
        "--input",
        "-",
        input_payload={
            "ref": ref,
            "inputs": {pr_input: str(number)},
        },
    )

    for attempt in range(8):
        after_payload = run_gh(
            f"repos/{repository}/actions/workflows/{workflow}/runs?event=workflow_dispatch&per_page=20"
        )
        candidates = [
            item
            for item in (after_payload or {}).get("workflow_runs", [])
            if isinstance(item, dict)
            and item.get("id") not in before_ids
            and item.get("head_branch") == ref
        ]
        if candidates:
            item = max(candidates, key=lambda value: int(value.get("id") or 0))
            return {
                "result": "DISPATCHED",
                "run_id": item.get("id"),
                "run_url": item.get("html_url"),
                "status": item.get("status"),
                "expected_head_sha": expected_head,
            }
        if attempt < 7:
            time.sleep(min(2 ** attempt, 8))

    raise RemediationError("dispatch aceito, mas novo run não foi confirmado")


def apply_plan(
    policy: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    policies = policy_by_repo(policy)
    results: list[dict[str, Any]] = []

    for action in plan.get("actions", []):
        repository = action.get("repository")
        repo_policy = policies.get(repository)
        if repo_policy is None:
            results.append(
                {
                    "action_id": action.get("action_id"),
                    "result": "BLOCKED",
                    "reason": "repository_not_in_policy",
                }
            )
            continue

        try:
            _validate_action_policy(action, repo_policy)
            if action["type"] == "update_branch":
                outcome = apply_update_branch(action, repo_policy)
            elif action["type"] == "dispatch_merge_queue":
                outcome = apply_dispatch_merge_queue(action, repo_policy)
            else:
                raise RemediationError("ação não suportada")
            results.append({"action_id": action["action_id"], **outcome})
        except (RemediationError, GovernanceControlPlaneError) as exc:
            results.append(
                {
                    "action_id": action.get("action_id"),
                    "result": "BLOCKED",
                    "reason": str(exc),
                }
            )

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_correlation_id": plan.get("source_correlation_id"),
        "results": results,
        "summary": {
            "total": len(results),
            "updated": sum(item.get("result") == "UPDATED" for item in results),
            "dispatched": sum(item.get("result") == "DISPATCHED" for item in results),
            "noop": sum(
                item.get("result") in {"NOOP", "STALE_NOOP"} for item in results
            ),
            "blocked": sum(item.get("result") == "BLOCKED" for item in results),
        },
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["plan", "apply"], default="plan")
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--result", type=Path, default=DEFAULT_RESULT)
    args = parser.parse_args()

    try:
        policy = load_json(args.policy)
        if args.mode == "plan":
            evidence = load_json(args.evidence)
            payload = plan_remediations(policy, evidence)
            write_json(args.plan, payload)
            print(
                "REPOSITORY_GOVERNANCE_REMEDIATION_PLAN "
                f"actions={payload['summary']['actions_total']} "
                f"update_branch={payload['summary']['update_branch']} "
                f"dispatch_merge_queue={payload['summary']['dispatch_merge_queue']} "
                f"blockers={payload['summary']['blockers_total']}"
            )
            return 0

        plan = load_json(args.plan)
        payload = apply_plan(policy, plan)
        write_json(args.result, payload)
        print(
            "REPOSITORY_GOVERNANCE_REMEDIATION_APPLY "
            f"total={payload['summary']['total']} "
            f"updated={payload['summary']['updated']} "
            f"dispatched={payload['summary']['dispatched']} "
            f"noop={payload['summary']['noop']} "
            f"blocked={payload['summary']['blocked']}"
        )
        return 0
    except RemediationError as exc:
        print(f"REPOSITORY_GOVERNANCE_REMEDIATION_FAILED: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

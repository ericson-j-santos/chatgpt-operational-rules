#!/usr/bin/env python3
"""Avalia snapshots de repositórios contra a política central.

Este módulo é puro/read-only: não acessa rede, não lê credenciais e não executa
mutações. Recebe um snapshot coletado do GitHub e produz decisões allow/hold/block
vinculadas ao head SHA de cada PR.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "config" / "repository-governance-control-plane.json"
DEFAULT_SNAPSHOT = ROOT / "evidence" / "repository-governance-snapshot.json"
DEFAULT_EVIDENCE = ROOT / "evidence" / "repository-governance-control-plane.json"
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class GovernanceControlPlaneError(RuntimeError):
    pass


def canonical_digest(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GovernanceControlPlaneError(f"JSON inválido em {path}: {exc}") from None


def validate_policy(policy: dict[str, Any]) -> None:
    if policy.get("schema_version") != 1:
        raise GovernanceControlPlaneError("schema_version da política deve ser 1")
    repositories = policy.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise GovernanceControlPlaneError("repositories deve ser lista não vazia")

    seen: set[str] = set()
    for item in repositories:
        if not isinstance(item, dict):
            raise GovernanceControlPlaneError("cada política de repositório deve ser objeto")
        repository = item.get("repository")
        if not isinstance(repository, str) or not REPO_RE.fullmatch(repository):
            raise GovernanceControlPlaneError(f"repositório inválido: {repository!r}")
        if repository in seen:
            raise GovernanceControlPlaneError(f"repositório duplicado: {repository}")
        seen.add(repository)
        if item.get("mode") not in {"enforce", "observe"}:
            raise GovernanceControlPlaneError(f"{repository}: mode inválido")
        if item.get("visibility") not in {"public", "private"}:
            raise GovernanceControlPlaneError(f"{repository}: visibility inválida")
        branch = item.get("default_branch")
        if not isinstance(branch, str) or not branch.strip():
            raise GovernanceControlPlaneError(f"{repository}: default_branch obrigatório")
        workflows = item.get("required_workflows")
        if not isinstance(workflows, list) or not all(
            isinstance(value, str) and value.strip() for value in workflows
        ):
            raise GovernanceControlPlaneError(
                f"{repository}: required_workflows deve ser lista de textos"
            )


def _utc(value: str | None) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def latest_required_workflow_states(
    runs: Iterable[dict[str, Any]],
    required_workflows: Iterable[str],
    *,
    head_sha: str,
) -> list[dict[str, Any]]:
    relevant = [
        run
        for run in runs
        if run.get("head_sha") == head_sha and run.get("event") == "pull_request"
    ]
    states: list[dict[str, Any]] = []
    for workflow in required_workflows:
        candidates = [run for run in relevant if run.get("name") == workflow]
        if not candidates:
            states.append(
                {
                    "workflow": workflow,
                    "state": "missing",
                    "run_id": None,
                    "url": None,
                }
            )
            continue

        latest = max(candidates, key=lambda run: _utc(run.get("created_at")))
        status = latest.get("status")
        conclusion = latest.get("conclusion")
        if status != "completed":
            state = "pending"
        elif conclusion == "success":
            state = "success"
        else:
            state = "failure"

        states.append(
            {
                "workflow": workflow,
                "state": state,
                "status": status,
                "conclusion": conclusion,
                "run_id": latest.get("id"),
                "url": latest.get("html_url"),
                "created_at": latest.get("created_at"),
                "updated_at": latest.get("updated_at"),
            }
        )
    return states


def decide_pull(
    *,
    draft: bool,
    mergeable: bool | None,
    mergeable_state: str | None,
    behind_by: int,
    require_up_to_date: bool,
    workflow_states: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    reasons: list[str] = []

    if draft:
        reasons.append("draft")
    if mergeable is False or mergeable_state == "dirty":
        reasons.append("conflict")
    if mergeable is None:
        reasons.append("mergeability_unknown")
    if require_up_to_date and behind_by > 0:
        reasons.append("behind_base")

    gate_failure = any(
        item["state"] in {"failure", "missing"} for item in workflow_states
    )
    gate_pending = any(item["state"] == "pending" for item in workflow_states)
    if gate_failure:
        reasons.append("required_gate_failed_or_missing")
    if gate_pending:
        reasons.append("required_gate_pending")

    if any(
        reason
        in {"conflict", "behind_base", "required_gate_failed_or_missing"}
        for reason in reasons
    ):
        return "block", reasons
    if reasons:
        return "hold", reasons
    return "allow", []


def evaluate_pull(
    policy: dict[str, Any],
    raw: dict[str, Any],
) -> dict[str, Any]:
    detail = raw.get("detail") or {}
    comparison = raw.get("comparison") or {}
    head_sha = str(((detail.get("head") or {}).get("sha") or "")).lower()
    if not SHA_RE.fullmatch(head_sha):
        raise GovernanceControlPlaneError(
            f"{policy['repository']} PR#{detail.get('number')}: head SHA inválido"
        )

    behind_by = int(comparison.get("behind_by", 0))
    workflow_states = latest_required_workflow_states(
        raw.get("workflow_runs") or [],
        policy.get("required_workflows", []),
        head_sha=head_sha,
    )
    decision, reasons = decide_pull(
        draft=bool(detail.get("draft")),
        mergeable=detail.get("mergeable"),
        mergeable_state=detail.get("mergeable_state"),
        behind_by=behind_by,
        require_up_to_date=bool(policy.get("require_up_to_date", True)),
        workflow_states=workflow_states,
    )

    return {
        "number": detail.get("number"),
        "title": detail.get("title"),
        "url": detail.get("html_url"),
        "head_ref": (detail.get("head") or {}).get("ref"),
        "head_sha": head_sha,
        "base_ref": (detail.get("base") or {}).get("ref"),
        "draft": bool(detail.get("draft")),
        "mergeable": detail.get("mergeable"),
        "mergeable_state": detail.get("mergeable_state"),
        "behind_by": behind_by,
        "required_workflows": workflow_states,
        "decision": decision,
        "reasons": reasons,
    }


def evaluate_repository(
    policy: dict[str, Any],
    raw: dict[str, Any],
) -> dict[str, Any]:
    repository = policy["repository"]
    if raw.get("error"):
        return {
            "repository": repository,
            "mode": policy["mode"],
            "visibility": policy["visibility"],
            "status": "unreachable",
            "violations": ["repository_unreachable"],
            "error": raw["error"],
            "open_pull_requests": [],
        }

    metadata = raw.get("metadata") or {}
    branch = raw.get("branch") or {}
    expected_branch = policy["default_branch"]
    observed_branch = metadata.get("default_branch")
    main_sha = str(((branch.get("commit") or {}).get("sha") or "")).lower()
    if not SHA_RE.fullmatch(main_sha):
        raise GovernanceControlPlaneError(f"{repository}: main SHA inválido")

    violations: list[str] = []
    if observed_branch != expected_branch:
        violations.append("default_branch_drift")
    if policy.get("require_branch_protection") and not branch.get("protected"):
        violations.append("branch_unprotected")
    if metadata.get("archived"):
        violations.append("repository_archived")

    pulls = [
        evaluate_pull(policy, item)
        for item in raw.get("pulls", [])
    ]
    return {
        "repository": repository,
        "mode": policy["mode"],
        "visibility": policy["visibility"],
        "default_branch": expected_branch,
        "main_sha": main_sha,
        "protected": bool(branch.get("protected")),
        "archived": bool(metadata.get("archived")),
        "allow_auto_merge": bool(metadata.get("allow_auto_merge")),
        "violations": violations,
        "status": "degraded" if violations else "healthy",
        "open_pull_requests": pulls,
    }


def build_summary(results: list[dict[str, Any]]) -> dict[str, int]:
    summary = {
        "repositories_total": len(results),
        "repositories_healthy": 0,
        "repositories_degraded": 0,
        "repositories_unreachable": 0,
        "pulls_allow": 0,
        "pulls_hold": 0,
        "pulls_block": 0,
    }
    for result in results:
        key = f"repositories_{result.get('status')}"
        if key in summary:
            summary[key] += 1
        for pull in result.get("open_pull_requests", []):
            pull_key = f"pulls_{pull.get('decision')}"
            if pull_key in summary:
                summary[pull_key] += 1
    return summary


def evaluate_control_plane(
    policy: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    validate_policy(policy)
    if snapshot.get("schema_version") != 1:
        raise GovernanceControlPlaneError("schema_version do snapshot deve ser 1")

    raw_by_repo = {
        item.get("repository"): item
        for item in snapshot.get("repositories", [])
        if isinstance(item, dict)
    }
    skipped_by_repo = {
        item.get("repository"): item
        for item in snapshot.get("skipped", [])
        if isinstance(item, dict)
    }

    results: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in policy["repositories"]:
        repository = item["repository"]
        if repository in skipped_by_repo:
            skipped.append(skipped_by_repo[repository])
            continue
        raw = raw_by_repo.get(repository)
        if raw is None:
            results.append(
                {
                    "repository": repository,
                    "mode": item["mode"],
                    "visibility": item["visibility"],
                    "status": "unreachable",
                    "violations": ["snapshot_missing"],
                    "error": "repositório ausente do snapshot",
                    "open_pull_requests": [],
                }
            )
            continue
        results.append(evaluate_repository(item, raw))

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "correlation_id": snapshot.get("correlation_id"),
        "snapshot_generated_at": snapshot.get("generated_at"),
        "policy_sha256": canonical_digest(policy),
        "summary": build_summary(results),
        "repositories": results,
        "skipped": skipped,
    }


def write_evidence(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def print_summary(payload: dict[str, Any]) -> None:
    summary = payload["summary"]
    print(
        "REPOSITORY_GOVERNANCE_AUDIT "
        f"repos={summary['repositories_total']} "
        f"healthy={summary['repositories_healthy']} "
        f"degraded={summary['repositories_degraded']} "
        f"unreachable={summary['repositories_unreachable']} "
        f"allow={summary['pulls_allow']} "
        f"hold={summary['pulls_hold']} "
        f"block={summary['pulls_block']}"
    )
    for repo in payload["repositories"]:
        print(
            f"{repo['repository']} status={repo['status']} "
            f"main_sha={repo.get('main_sha', '-')}"
        )
        for pull in repo.get("open_pull_requests", []):
            reasons = ",".join(pull["reasons"]) or "-"
            print(
                f"  PR#{pull['number']} decision={pull['decision']} "
                f"head_sha={pull['head_sha']} reasons={reasons}"
            )
    for item in payload.get("skipped", []):
        print(
            f"{item.get('repository')} status={item.get('status')} "
            f"reason={item.get('reason')}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    args = parser.parse_args()

    try:
        policy = load_json(args.policy)
        snapshot = load_json(args.snapshot)
        payload = evaluate_control_plane(policy, snapshot)
        write_evidence(args.evidence, payload)
        print_summary(payload)
        return 0
    except GovernanceControlPlaneError as exc:
        print(f"REPOSITORY_GOVERNANCE_FAILED: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

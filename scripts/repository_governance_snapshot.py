#!/usr/bin/env python3
"""Coleta snapshot read-only do GitHub para o Repository Governance Control Plane.

A autenticação é responsabilidade do cliente oficial `gh` no ambiente de
execução. Este módulo não lê, imprime nem persiste credenciais.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "config" / "repository-governance-control-plane.json"
DEFAULT_OUTPUT = ROOT / "evidence" / "repository-governance-snapshot.json"
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class SnapshotError(RuntimeError):
    pass


def gh_json(path: str) -> Any:
    completed = subprocess.run(
        ["gh", "api", path],
        text=True,
        capture_output=True,
        check=False,
        timeout=45,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "gh api failed"
        raise SnapshotError(message[:500])
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SnapshotError(f"resposta JSON inválida de gh api: {exc}") from None


def load_policy(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"política inválida: {exc}") from None
    repositories = value.get("repositories")
    if value.get("schema_version") != 1 or not isinstance(repositories, list):
        raise SnapshotError("política com schema inválido")
    return value


def repo_api(repository: str) -> str:
    if not REPO_RE.fullmatch(repository):
        raise SnapshotError(f"repositório inválido: {repository!r}")
    owner, name = repository.split("/", 1)
    return "/".join(
        ["repos", urllib.parse.quote(owner, safe=""), urllib.parse.quote(name, safe="")]
    )


def collect_pull(repository: str, number: int, base_branch: str) -> dict[str, Any]:
    base = repo_api(repository)
    detail = gh_json(f"{base}/pulls/{number}")
    head_sha = ((detail.get("head") or {}).get("sha") or "").strip()
    comparison = gh_json(
        f"{base}/compare/"
        f"{urllib.parse.quote(base_branch, safe='')}..."
        f"{urllib.parse.quote(head_sha, safe='')}"
    )
    query = urllib.parse.urlencode(
        {
            "head_sha": head_sha,
            "event": "pull_request",
            "per_page": 100,
        }
    )
    runs_payload = gh_json(f"{base}/actions/runs?{query}")
    return {
        "detail": detail,
        "comparison": comparison,
        "workflow_runs": runs_payload.get("workflow_runs", []),
    }


def collect_repository(policy: dict[str, Any]) -> dict[str, Any]:
    repository = policy["repository"]
    base = repo_api(repository)
    try:
        metadata = gh_json(base)
        branch_name = policy["default_branch"]
        branch = gh_json(
            f"{base}/branches/{urllib.parse.quote(branch_name, safe='')}"
        )
        pulls = gh_json(f"{base}/pulls?state=open&per_page=100")
        if not isinstance(pulls, list):
            raise SnapshotError("lista de PRs inválida")
        detailed = [
            collect_pull(repository, int(item["number"]), branch_name)
            for item in pulls
            if isinstance(item.get("number"), int)
        ]
        return {
            "repository": repository,
            "metadata": metadata,
            "branch": branch,
            "pulls": detailed,
        }
    except SnapshotError as exc:
        return {
            "repository": repository,
            "error": str(exc),
        }


def collect_snapshot(
    policy: dict[str, Any],
    *,
    public_only: bool,
    repositories: set[str] | None = None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in policy["repositories"]:
        repository = item["repository"]
        if repositories and repository not in repositories:
            continue
        if public_only and item.get("visibility") != "public":
            skipped.append(
                {
                    "repository": repository,
                    "mode": item.get("mode"),
                    "status": "skipped_private",
                    "reason": "cross_repository_token_unavailable",
                }
            )
            continue
        results.append(collect_repository(item))

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "correlation_id": correlation_id,
        "public_only": public_only,
        "repositories": results,
        "skipped": skipped,
    }


def write_snapshot(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--public-only", action="store_true")
    parser.add_argument("--repository", action="append", default=[])
    parser.add_argument("--correlation-id")
    args = parser.parse_args()

    try:
        policy = load_policy(args.policy)
        payload = collect_snapshot(
            policy,
            public_only=args.public_only,
            repositories=set(args.repository) or None,
            correlation_id=args.correlation_id,
        )
        write_snapshot(args.output, payload)
        print(
            "REPOSITORY_GOVERNANCE_SNAPSHOT "
            f"repos={len(payload['repositories'])} "
            f"skipped={len(payload['skipped'])}"
        )
        return 0
    except SnapshotError as exc:
        print(f"REPOSITORY_GOVERNANCE_SNAPSHOT_FAILED: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Dispara workflow_dispatch do GitHub de forma restrita e auditável."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from itertools import product
from urllib.parse import quote

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
WORKFLOW_RE = re.compile(r"^[A-Za-z0-9_.-]+\.(?:yml|yaml)$")
REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
CORRELATION_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
REQSYS_REPO = "ericson-j-santos/reqsys-v2-enterprise-real"
REQSYS_WORKFLOWS = {
    "planner-teams-notify-dev-acceptance.yml",
    "teams-dashboard-availability-monitor.yml",
    "reqsys-weekly-accomplishment-log.yml",
}
REQSYS_REFS = {
    "main",
    "fix/1358-monitor-contract-diagnostics-20260917",
    "fix/1358-monitor-dispatch-trigger-20260917",
}
TODO22_TARGET = (
    "ericson-j-santos/chatgpt-operational-rules",
    "validate-rules.yml",
    "fix/todo22-installer-isolated-gate-20261019",
)
TODO22_MAIN_TARGET = (
    "ericson-j-santos/chatgpt-operational-rules",
    "validate-rules.yml",
    "main",
)
TODO22_TARGETS = frozenset({TODO22_TARGET, TODO22_MAIN_TARGET})
NOTERI_EVENTLOG_TARGET = (
    "ericson-j-santos/noteri-runtime",
    "noteri-sql-application-eventlog.yml",
    "main",
)
ALLOWED_TARGETS = frozenset(
    (REQSYS_REPO, workflow, ref)
    for workflow, ref in product(REQSYS_WORKFLOWS, REQSYS_REFS)
) | TODO22_TARGETS | frozenset({NOTERI_EVENTLOG_TARGET})
PR_BOUND_TARGETS = {
    TODO22_TARGET: {"base": "main", "head_owner": "ericson-j-santos"},
}
DISPATCH_CONFIRM_ATTEMPTS = 6
DISPATCH_CONFIRM_INITIAL_DELAY_SECONDS = 1
DISPATCH_CONFIRM_MAX_DELAY_SECONDS = 8


def run(*args: str) -> str:
    cp = subprocess.run(list(args), text=True, capture_output=True, timeout=60, check=False)
    if cp.returncode:
        raise RuntimeError(f"github command failed rc={cp.returncode}")
    if len(cp.stdout) > 1_000_000:
        raise RuntimeError("github response exceeded limit")
    return cp.stdout.strip()


def validate(
    repo: str,
    workflow: str,
    ref: str,
    expected_head: str,
) -> tuple[str, str, str, str]:
    repo, workflow, ref = repo.strip(), workflow.strip(), ref.strip()
    expected_head = expected_head.strip().lower()
    if not REPO_RE.fullmatch(repo):
        raise ValueError("repositório inválido")
    if not WORKFLOW_RE.fullmatch(workflow):
        raise ValueError("workflow inválido")
    if not REF_RE.fullmatch(ref):
        raise ValueError("ref inválida")
    if (repo, workflow, ref) not in ALLOWED_TARGETS:
        raise ValueError("alvo fora da allowlist")
    if not SHA_RE.fullmatch(expected_head):
        raise ValueError("HEAD esperado inválido")
    return repo, workflow, ref, expected_head


def _json_object(raw: str) -> dict:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError("github response invalid")
    return value


def _json_array(raw: str) -> list:
    value = json.loads(raw)
    if not isinstance(value, list):
        raise RuntimeError("github response invalid")
    return value


def _workflow_runs(raw: str) -> list[dict]:
    runs = _json_object(raw).get("workflow_runs")
    if (
        not isinstance(runs, list)
        or not all(
            isinstance(item, dict) and isinstance(item.get("id"), int)
            for item in runs
        )
    ):
        raise RuntimeError("github workflow runs response invalid")
    return runs


def verify_remote_head(
    repo: str,
    workflow: str,
    ref: str,
    expected_head: str,
) -> int | None:
    target = (repo, workflow, ref)
    pr_policy = PR_BOUND_TARGETS.get(target)
    if pr_policy is not None:
        head = quote(f"{pr_policy['head_owner']}:{ref}", safe="")
        base = quote(pr_policy["base"], safe="")
        pulls = _json_array(
            run(
                "gh",
                "api",
                f"repos/{repo}/pulls?state=open&base={base}&head={head}&per_page=10",
            )
        )
        if len(pulls) != 1 or not isinstance(pulls[0], dict):
            raise RuntimeError("unique draft pull request verification failed")
        pull = pulls[0]
        head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
        base = pull.get("base") if isinstance(pull.get("base"), dict) else {}
        head_repo = head.get("repo") if isinstance(head.get("repo"), dict) else {}
        if (
            not isinstance(pull.get("number"), int)
            or pull.get("state") != "open"
            or pull.get("draft") is not True
            or base.get("ref") != pr_policy["base"]
            or head_repo.get("full_name") != repo
            or head.get("ref") != ref
            or head.get("sha") != expected_head
        ):
            raise RuntimeError("pull request target verification failed")
        return pull["number"]

    encoded_ref = quote(f"heads/{ref}", safe="")
    remote_ref = _json_object(run("gh", "api", f"repos/{repo}/git/ref/{encoded_ref}"))
    obj = remote_ref.get("object") if isinstance(remote_ref.get("object"), dict) else {}
    if obj.get("sha") != expected_head:
        raise RuntimeError("remote HEAD verification failed")
    return None


def dispatch(
    repo: str,
    workflow: str,
    ref: str,
    correlation_id: str,
    expected_head: str,
) -> dict:
    repo, workflow, ref, expected_head = validate(repo, workflow, ref, expected_head)
    correlation_id = correlation_id.strip()
    if not CORRELATION_RE.fullmatch(correlation_id):
        raise ValueError("identificador de correlação inválido")
    pull_request = verify_remote_head(repo, workflow, ref, expected_head)
    expected_display_title = (
        f"Operational Rules / {correlation_id}"
        if (repo, workflow, ref) in TODO22_TARGETS
        else None
    )
    before = run("gh", "api", f"repos/{repo}/actions/workflows/{workflow}/runs?event=workflow_dispatch&per_page=10")
    before_ids = {r["id"] for r in _workflow_runs(before)}
    dispatch_args = [
        "gh",
        "api",
        "--method",
        "POST",
        f"repos/{repo}/actions/workflows/{workflow}/dispatches",
        "-f",
        f"ref={ref}",
    ]
    if (repo, workflow, ref) in TODO22_TARGETS:
        dispatch_args.extend(
            (
                "-f",
                f"inputs[expected_sha]={expected_head}",
                "-f",
                f"inputs[correlation_id]={correlation_id}",
            )
        )
    run(*dispatch_args)
    candidates = []
    for attempt in range(DISPATCH_CONFIRM_ATTEMPTS):
        after = run("gh", "api", f"repos/{repo}/actions/workflows/{workflow}/runs?event=workflow_dispatch&per_page=10")
        candidates = [
            r
            for r in _workflow_runs(after)
            if r["id"] not in before_ids
            and r.get("head_branch") == ref
            and r.get("head_sha") == expected_head
            and (
                expected_display_title is None
                or r.get("display_title") == expected_display_title
            )
        ]
        if len(candidates) > 1:
            raise RuntimeError("dispatch confirmation ambiguous")
        if len(candidates) == 1:
            break
        if attempt < DISPATCH_CONFIRM_ATTEMPTS - 1:
            delay = min(
                DISPATCH_CONFIRM_INITIAL_DELAY_SECONDS * (2**attempt),
                DISPATCH_CONFIRM_MAX_DELAY_SECONDS,
            )
            time.sleep(delay)
    if not candidates:
        raise RuntimeError(
            "dispatch aceito, mas run_id novo não foi confirmado após retentativas limitadas"
        )
    item = max(candidates, key=lambda r: r["id"])
    result = {
        "result": "DISPATCHED",
        "correlation_id": correlation_id,
        "repository": repo,
        "workflow": workflow,
        "ref": ref,
        "head_sha": expected_head,
        "run_id": item["id"],
        "run_url": item.get("html_url"),
        "status": item.get("status"),
    }
    if pull_request is not None:
        result["pull_request"] = pull_request
    return result


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--repo", required=True); p.add_argument("--workflow", required=True)
    p.add_argument("--ref", default="main"); p.add_argument("--correlation-id", required=True)
    p.add_argument("--expected-head", required=True)
    ns = p.parse_args()
    try:
        print(json.dumps(dispatch(ns.repo, ns.workflow, ns.ref, ns.correlation_id, ns.expected_head), ensure_ascii=False)); return 0
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print(json.dumps({"result": "BLOCKED", "error": str(exc)}, ensure_ascii=False), file=sys.stderr); return 31


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Dispara workflow_dispatch do GitHub de forma restrita e auditável."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
WORKFLOW_RE = re.compile(r"^[A-Za-z0-9_.-]+\.(?:yml|yaml)$")
REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
ALLOWED_WORKFLOWS = {"planner-teams-notify-dev-acceptance.yml", "teams-dashboard-availability-monitor.yml"}
ALLOWED_REPOS = {"ericson-j-santos/reqsys-v2-enterprise-real"}
ALLOWED_REFS = {"main", "fix/1358-monitor-contract-diagnostics-20260917", "fix/1358-monitor-dispatch-trigger-20260917"}


def run(*args: str) -> str:
    cp = subprocess.run(list(args), text=True, capture_output=True, timeout=60, check=False)
    if cp.returncode:
        raise RuntimeError((cp.stderr or cp.stdout or "command failed").strip())
    return cp.stdout.strip()


def validate(repo: str, workflow: str, ref: str) -> tuple[str, str, str]:
    repo, workflow, ref = repo.strip(), workflow.strip(), ref.strip()
    if not REPO_RE.fullmatch(repo) or repo not in ALLOWED_REPOS:
        raise ValueError("repositório fora da allowlist")
    if not WORKFLOW_RE.fullmatch(workflow) or workflow not in ALLOWED_WORKFLOWS:
        raise ValueError("workflow fora da allowlist")
    if not REF_RE.fullmatch(ref) or ref not in ALLOWED_REFS:
        raise ValueError("ref fora da allowlist")
    return repo, workflow, ref


def dispatch(repo: str, workflow: str, ref: str, correlation_id: str) -> dict:
    repo, workflow, ref = validate(repo, workflow, ref)
    before = run("gh", "api", f"repos/{repo}/actions/workflows/{workflow}/runs?event=workflow_dispatch&per_page=10")
    before_ids = {r["id"] for r in json.loads(before).get("workflow_runs", [])}
    run("gh", "api", "--method", "POST", f"repos/{repo}/actions/workflows/{workflow}/dispatches", "-f", f"ref={ref}")
    after = run("gh", "api", f"repos/{repo}/actions/workflows/{workflow}/runs?event=workflow_dispatch&per_page=10")
    candidates = [r for r in json.loads(after).get("workflow_runs", []) if r["id"] not in before_ids and r.get("head_branch") == ref]
    if not candidates:
        raise RuntimeError("dispatch aceito, mas run_id novo não foi confirmado")
    item = max(candidates, key=lambda r: r["id"])
    return {"result": "DISPATCHED", "correlation_id": correlation_id, "repository": repo, "workflow": workflow,
            "ref": ref, "run_id": item["id"], "run_url": item.get("html_url"), "status": item.get("status")}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--repo", required=True); p.add_argument("--workflow", required=True)
    p.add_argument("--ref", default="main"); p.add_argument("--correlation-id", required=True)
    ns = p.parse_args()
    try:
        print(json.dumps(dispatch(ns.repo, ns.workflow, ns.ref, ns.correlation_id), ensure_ascii=False)); return 0
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print(json.dumps({"result": "BLOCKED", "error": str(exc)}, ensure_ascii=False), file=sys.stderr); return 31


if __name__ == "__main__":
    raise SystemExit(main())

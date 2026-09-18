#!/usr/bin/env python3
"""Ponte idempotente GitHub Actions -> TODO Gateway para o ciclo horário do TODO Global."""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from scripts.todo_event_bus import make_idempotency_key, utc_now_iso

DEFAULT_REPOSITORY = "ericson-j-santos/chatgpt-operational-rules"
DEFAULT_WORKFLOW = "todo-global-hourly-cycle.yml"
DEFAULT_BRANCH = "main"
DEFAULT_GATEWAY_URL = "http://127.0.0.1:8094"
DEFAULT_STATE_FILE = "todo-global-hourly-bridge-state.json"
TERMINAL_STATUSES = {"CONCLUÍDO", "CANCELADO"}
ALLOWED_RUN_EVENTS = {"schedule", "workflow_dispatch"}


@dataclass(frozen=True)
class WorkflowRun:
    run_id: int
    event: str
    head_branch: str
    head_sha: str
    html_url: str
    created_at: str


class BridgeError(RuntimeError):
    pass


def _json_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    body = None
    effective_headers = {"Accept": "application/json", "User-Agent": "todo-global-hourly-bridge/1"}
    if headers:
        effective_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        effective_headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=effective_headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(262144)
            data = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(data, dict):
                raise BridgeError("JSON response must be an object")
            return data
    except HTTPError as exc:
        raise BridgeError(f"HTTP {exc.code} calling {url.split('?')[0]}") from exc
    except (URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
        raise BridgeError(f"request failed for {url.split('?')[0]}: {type(exc).__name__}") from exc


def _github_headers(token: str) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def latest_successful_run(
    repository: str,
    workflow: str,
    branch: str,
    *,
    github_token: str = "",
) -> WorkflowRun | None:
    workflow_ref = quote(workflow, safe="")
    query = urlencode({"status": "success", "per_page": 20})
    url = f"https://api.github.com/repos/{repository}/actions/workflows/{workflow_ref}/runs?{query}"
    data = _json_request("GET", url, headers=_github_headers(github_token))
    runs = data.get("workflow_runs") or []
    if not isinstance(runs, list):
        raise BridgeError("GitHub workflow_runs must be a list")
    eligible = [
        item
        for item in runs
        if isinstance(item, dict)
        and item.get("event") in ALLOWED_RUN_EVENTS
        and item.get("head_branch") == branch
        and item.get("conclusion") == "success"
    ]
    if not eligible:
        return None
    item = max(eligible, key=lambda value: int(value.get("id") or 0))
    return WorkflowRun(
        run_id=int(item["id"]),
        event=str(item["event"]),
        head_branch=str(item["head_branch"]),
        head_sha=str(item.get("head_sha") or ""),
        html_url=str(item.get("html_url") or ""),
        created_at=str(item.get("created_at") or ""),
    )


def _gateway_json(
    method: str,
    gateway_url: str,
    token: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not token:
        raise BridgeError("TODO_GATEWAY_TOKEN is required")
    url = gateway_url.rstrip("/") + path
    return _json_request(
        method,
        url,
        headers={"Authorization": f"Bearer {token}"},
        payload=payload,
    )


def _scheduler_event(run: WorkflowRun) -> tuple[dict[str, Any], str]:
    external_id = "todo-global-hourly-cycle"
    key = make_idempotency_key("AI Control Plane", "Automação", external_id)
    correlation_id = f"gh-hourly-{run.run_id}"
    event = {
        "schema_version": "1.0",
        "event_id": f"evt-gh-hourly-{run.run_id}",
        "event_type": "todo.updated",
        "occurred_at": utc_now_iso(),
        "correlation_id": correlation_id,
        "idempotency_key": key,
        "project": "AI Control Plane",
        "producer": "github-actions-hourly-bridge",
        "todo": {
            "title": "Ciclo horário governado do TODO Global",
            "type": "Automação",
            "external_id": external_id,
            "status": "EM ANDAMENTO",
            "priority": "P1",
            "source": "GitHub",
            "origin": "GitHub Actions",
            "origin_url": run.html_url or None,
            "next_action": "reconciliar e continuar TODOs tipados executáveis",
            "evidence": f"workflow_run_id={run.run_id};head_sha={run.head_sha}",
        },
    }
    return event, key


def process_tick(
    run: WorkflowRun,
    gateway_url: str,
    gateway_token: str,
    *,
    only_keys: set[str] | None = None,
) -> dict[str, Any]:
    event, scheduler_key = _scheduler_event(run)
    publish = _gateway_json("POST", gateway_url, gateway_token, "/v1/events", event)

    todos = _gateway_json("GET", gateway_url, gateway_token, "/v1/todos?limit=200")
    continuations = _gateway_json("GET", gateway_url, gateway_token, "/v1/continuations?limit=200")
    continuation_basis = {
        str(item.get("basis_event_id") or "")
        for item in continuations.get("items", [])
        if isinstance(item, dict)
    }

    requested = 0
    duplicate_or_existing = 0
    skipped_terminal = 0
    skipped_untyped = 0
    failures = 0
    details: list[dict[str, str]] = []

    for item in todos.get("items", []):
        if not isinstance(item, dict):
            continue
        key = str(item.get("idempotency_key") or "")
        if not key or key == scheduler_key:
            continue
        if only_keys is not None and key not in only_keys:
            continue
        todo = item.get("todo") or {}
        if not isinstance(todo, dict):
            continue
        status = str(todo.get("status") or "")
        if status in TERMINAL_STATUSES:
            skipped_terminal += 1
            continue
        action = str(todo.get("automation_action") or "").strip()
        if not action:
            skipped_untyped += 1
            continue
        basis_event_id = str(item.get("event_id") or "")
        if basis_event_id and basis_event_id in continuation_basis:
            duplicate_or_existing += 1
            continue
        correlation_id = f"gh-hourly-{run.run_id}-{key[:12]}"
        try:
            result = _gateway_json(
                "POST",
                gateway_url,
                gateway_token,
                f"/v1/todos/{quote(key, safe='')}/continue",
                {"correlation_id": correlation_id},
            )
        except BridgeError as exc:
            failures += 1
            details.append({"idempotency_key": key, "result": "error", "detail": str(exc)[:160]})
            continue
        if bool(result.get("duplicate")):
            duplicate_or_existing += 1
        else:
            requested += 1
        details.append(
            {
                "idempotency_key": key,
                "request_id": str(result.get("request_id") or ""),
                "result": "duplicate" if result.get("duplicate") else "requested",
            }
        )

    if failures:
        raise BridgeError(f"continuation request failures={failures}")

    return {
        "run_id": run.run_id,
        "run_event": run.event,
        "run_url": run.html_url,
        "head_sha": run.head_sha,
        "scheduler_event_id": event["event_id"],
        "scheduler_event_duplicate": bool(publish.get("duplicate")),
        "requested": requested,
        "duplicate_or_existing": duplicate_or_existing,
        "skipped_terminal": skipped_terminal,
        "skipped_untyped": skipped_untyped,
        "details": details,
    }


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise BridgeError("state file must contain a JSON object")
    return data


def save_state(path: Path, run: WorkflowRun, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = {
        "last_run_id": run.run_id,
        "last_run_url": run.html_url,
        "last_head_sha": run.head_sha,
        "processed_at": utc_now_iso(),
        "result": {
            "requested": result["requested"],
            "duplicate_or_existing": result["duplicate_or_existing"],
            "skipped_terminal": result["skipped_terminal"],
            "skipped_untyped": result["skipped_untyped"],
        },
    }
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    tmp.replace(path)


def run_once(
    *,
    repository: str,
    workflow: str,
    branch: str,
    gateway_url: str,
    gateway_token: str,
    state_file: Path,
    github_token: str = "",
) -> dict[str, Any]:
    run = latest_successful_run(repository, workflow, branch, github_token=github_token)
    if run is None:
        return {"result": "NO_ELIGIBLE_RUN"}

    state = load_state(state_file)
    if int(state.get("last_run_id") or 0) == run.run_id:
        return {"result": "ALREADY_PROCESSED", "run_id": run.run_id}

    result = process_tick(run, gateway_url, gateway_token)
    save_state(state_file, run, result)
    return {"result": "PROCESSED", **result}


def main() -> int:
    parser = argparse.ArgumentParser(description="Ponte GitHub Actions -> TODO Gateway")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_SCHEDULE_REPOSITORY", DEFAULT_REPOSITORY))
    parser.add_argument("--workflow", default=os.environ.get("GITHUB_SCHEDULE_WORKFLOW", DEFAULT_WORKFLOW))
    parser.add_argument("--branch", default=os.environ.get("GITHUB_SCHEDULE_BRANCH", DEFAULT_BRANCH))
    parser.add_argument("--gateway-url", default=os.environ.get("TODO_GATEWAY_URL", DEFAULT_GATEWAY_URL))
    parser.add_argument("--state-file", type=Path, default=Path(os.environ.get("GITHUB_SCHEDULE_STATE_FILE", DEFAULT_STATE_FILE)))
    args = parser.parse_args()

    interval = max(30, min(args.interval_seconds, 900))
    token = os.environ.get("TODO_GATEWAY_TOKEN", "")
    github_token = os.environ.get("GITHUB_TOKEN", "")

    while True:
        try:
            result = run_once(
                repository=args.repository,
                workflow=args.workflow,
                branch=args.branch,
                gateway_url=args.gateway_url,
                gateway_token=token,
                state_file=args.state_file,
                github_token=github_token,
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
        except (BridgeError, OSError, ValueError, json.JSONDecodeError) as exc:
            print(
                json.dumps(
                    {"result": "ERROR", "error": str(exc)[:240]},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                flush=True,
            )
            if args.once:
                return 1

        if args.once:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())

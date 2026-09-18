#!/usr/bin/env python3
"""E2E controlado da ponte GitHub horário -> TODO Gateway -> worker PC24x7."""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from datetime import UTC, datetime

from scripts.github_schedule_bridge import WorkflowRun, process_tick
from scripts.todo_event_bus import make_idempotency_key, utc_now_iso
from scripts.todo_gateway_pc24x7_e2e import api_json, load_runtime_env, psql_scalar

PROJECT = "AI Control Plane"
GATEWAY_URL = "http://127.0.0.1:8094"


def current_head() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("git head unavailable")
    head = result.stdout.strip().lower()
    if len(head) != 40:
        raise RuntimeError("invalid git head")
    return head


def post_event(token: str, event: dict) -> dict:
    status, payload = api_json("POST", "/v1/events", token, event)
    if status != 202 or payload.get("accepted") is not True:
        raise RuntimeError(f"event rejected status={status}")
    return payload


def wait_continuation(token: str, basis_event_id: str, timeout_seconds: int = 30) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last: dict = {}
    while time.monotonic() < deadline:
        status, payload = api_json("GET", "/v1/continuations?limit=200", token)
        if status != 200:
            raise RuntimeError(f"continuation read failed status={status}")
        for item in payload.get("items", []):
            if item.get("basis_event_id") == basis_event_id:
                last = item
                if item.get("state") in {"COMPLETED", "HUMAN_GATE", "DLQ"}:
                    return item
        time.sleep(1)
    return last


def terminal_update(
    *,
    token: str,
    key: str,
    external_id: str,
    event_id: str,
    correlation_id: str,
    status: str,
    evidence: str,
) -> None:
    todo = {
        "title": "E2E controlado da agenda horária",
        "type": "Automação",
        "external_id": external_id,
        "status": status,
        "priority": "P2",
        "source": "GitHub",
        "next_action": "nenhuma",
    }
    if status == "CONCLUÍDO":
        todo["completion_criteria"] = "agenda, fila, worker e replay validados"
        todo["evidence"] = evidence
        todo["e2e_status"] = "VALIDADO"
    event = {
        "schema_version": "1.0",
        "event_id": event_id,
        "event_type": "todo.updated",
        "occurred_at": utc_now_iso(),
        "correlation_id": correlation_id,
        "idempotency_key": key,
        "project": PROJECT,
        "producer": "github-hourly-bridge-e2e",
        "todo": todo,
    }
    post_event(token, event)


def main() -> int:
    token = load_runtime_env()["TODO_GATEWAY_TOKEN"]
    suffix = uuid.uuid4().hex[:12]
    head = current_head()
    run_seed = int(datetime.now(UTC).strftime("%Y%m%d%H%M%S"))

    positive_external = f"desktop-24x7-e2e-hourly-{suffix}"
    positive_key = make_idempotency_key(PROJECT, "Automação", positive_external)
    positive_event_id = f"evt-hourly-positive-{suffix}"
    positive_corr = f"corr-hourly-positive-{suffix}"
    positive_event = {
        "schema_version": "1.0",
        "event_id": positive_event_id,
        "event_type": "todo.updated",
        "occurred_at": utc_now_iso(),
        "correlation_id": positive_corr,
        "idempotency_key": positive_key,
        "project": PROJECT,
        "producer": "github-hourly-bridge-e2e",
        "todo": {
            "title": "E2E controlado da agenda horária",
            "type": "Automação",
            "external_id": positive_external,
            "status": "EM ANDAMENTO",
            "priority": "P2",
            "source": "GitHub",
            "next_action": "validar continuidade idempotente",
            "automation_action": "ai_control_plane.validate_idempotent_continuation.v1",
        },
    }
    first_post = post_event(token, positive_event)

    run = WorkflowRun(
        run_id=run_seed,
        event="workflow_dispatch",
        head_branch="main",
        head_sha=head,
        html_url=f"https://github.com/{PROJECT.replace(' ', '-').lower()}/e2e/{run_seed}",
        created_at=utc_now_iso(),
    )
    first_cycle = process_tick(run, GATEWAY_URL, token, only_keys={positive_key})
    terminal = wait_continuation(token, positive_event_id)
    replay_cycle = process_tick(run, GATEWAY_URL, token, only_keys={positive_key})

    positive_event_count = psql_scalar(
        "SELECT count(*) FROM todo_bus.queue_events "
        f"WHERE event_id = '{positive_event_id}'"
    )
    positive_continuation_count = psql_scalar(
        "SELECT count(*) FROM todo_bus.continuation_requests "
        f"WHERE basis_event_id = '{positive_event_id}'"
    )

    negative_external = f"desktop-negative-hourly-{suffix}"
    negative_key = make_idempotency_key(PROJECT, "Automação", negative_external)
    negative_event_id = f"evt-hourly-negative-{suffix}"
    negative_event = {
        "schema_version": "1.0",
        "event_id": negative_event_id,
        "event_type": "todo.updated",
        "occurred_at": utc_now_iso(),
        "correlation_id": f"corr-hourly-negative-{suffix}",
        "idempotency_key": negative_key,
        "project": PROJECT,
        "producer": "github-hourly-bridge-e2e",
        "todo": {
            "title": "Controle negativo da agenda horária",
            "type": "Automação",
            "external_id": negative_external,
            "status": "PENDENTE",
            "priority": "P2",
            "source": "GitHub",
            "next_action": "não deve gerar continuação sem ação tipada",
        },
    }
    post_event(token, negative_event)
    negative_run = WorkflowRun(
        run_id=run_seed + 1,
        event="workflow_dispatch",
        head_branch="main",
        head_sha=head,
        html_url=f"https://github.com/ai-control-plane/e2e/{run_seed + 1}",
        created_at=utc_now_iso(),
    )
    negative_cycle = process_tick(negative_run, GATEWAY_URL, token, only_keys={negative_key})
    negative_continuation_count = psql_scalar(
        "SELECT count(*) FROM todo_bus.continuation_requests "
        f"WHERE basis_event_id = '{negative_event_id}'"
    )

    ready = all(
        [
            first_post.get("duplicate") is False,
            first_cycle["requested"] == 1,
            terminal.get("state") == "COMPLETED",
            replay_cycle["requested"] == 0,
            replay_cycle["duplicate_or_existing"] == 1,
            replay_cycle["scheduler_event_duplicate"] is True,
            positive_event_count == "1",
            positive_continuation_count == "1",
            negative_cycle["requested"] == 0,
            negative_cycle["skipped_untyped"] == 1,
            negative_continuation_count == "0",
        ]
    )

    evidence = {
        "contract": "github-hourly-todo-bridge-e2e",
        "generated_at": datetime.now(UTC).isoformat(),
        "head_sha": head,
        "positive_event_id": positive_event_id,
        "positive_idempotency_key": positive_key,
        "positive_continuation_state": terminal.get("state"),
        "positive_event_count": int(positive_event_count),
        "positive_continuation_count": int(positive_continuation_count),
        "first_cycle_requested": first_cycle["requested"],
        "replay_cycle_requested": replay_cycle["requested"],
        "replay_existing": replay_cycle["duplicate_or_existing"],
        "scheduler_replay_duplicate": replay_cycle["scheduler_event_duplicate"],
        "negative_event_id": negative_event_id,
        "negative_requested": negative_cycle["requested"],
        "negative_skipped_untyped": negative_cycle["skipped_untyped"],
        "negative_continuation_count": int(negative_continuation_count),
        "ready": ready,
    }

    if ready:
        terminal_update(
            token=token,
            key=positive_key,
            external_id=positive_external,
            event_id=f"evt-hourly-positive-done-{suffix}",
            correlation_id=positive_corr,
            status="CONCLUÍDO",
            evidence=json.dumps(evidence, ensure_ascii=False, sort_keys=True),
        )
        terminal_update(
            token=token,
            key=negative_key,
            external_id=negative_external,
            event_id=f"evt-hourly-negative-cancel-{suffix}",
            correlation_id=f"corr-hourly-negative-{suffix}",
            status="CANCELADO",
            evidence="controle negativo concluído",
        )

    print(json.dumps(evidence, ensure_ascii=True, sort_keys=True))
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())

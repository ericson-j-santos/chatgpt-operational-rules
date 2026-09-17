#!/usr/bin/env python3
"""Consumidor contínuo da fila governada de continuação de TODOs."""
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Continuation:
    request_id: str
    idempotency_key: str
    correlation_id: str
    project: str
    todo: dict[str, Any]
    attempts: int


class Queue(Protocol):
    def reserve_continuations(self, limit: int, lease_seconds: int) -> list[Continuation]: ...
    def complete_continuation(self, request_id: str) -> bool: ...
    def gate_continuation(self, request_id: str, detail: str) -> bool: ...
    def fail_continuation(self, request_id: str, error: str, max_attempts: int, backoff_seconds: int) -> str: ...


class PostgresContinuationQueue:
    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError("DATABASE_URL is required")
        self.dsn = dsn

    def _connect(self):
        import psycopg
        return psycopg.connect(self.dsn)

    def reserve_continuations(self, limit: int, lease_seconds: int) -> list[Continuation]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM todo_bus.reserve_continuations(%s,%s)", (limit, lease_seconds))
            rows = cur.fetchall()
        return [Continuation(str(r[0]), str(r[1]), str(r[3]), str(r[4]), dict(r[5]), int(r[7])) for r in rows]

    def _transition(self, sql: str, params: tuple[Any, ...]) -> Any:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()[0]

    def complete_continuation(self, request_id: str) -> bool:
        return bool(self._transition("SELECT todo_bus.complete_continuation(%s)", (request_id,)))

    def gate_continuation(self, request_id: str, detail: str) -> bool:
        return bool(self._transition("SELECT todo_bus.gate_continuation(%s,%s)", (request_id, detail)))

    def fail_continuation(self, request_id: str, error: str, max_attempts: int, backoff_seconds: int) -> str:
        return str(self._transition(
            "SELECT todo_bus.fail_continuation(%s,%s,%s,%s)",
            (request_id, error, max_attempts, backoff_seconds),
        ))

    def heartbeat(self, worker_id: str, counts: dict[str, int]) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT todo_bus.record_worker_heartbeat(%s,%s::jsonb)",
                        (worker_id, json.dumps(counts, sort_keys=True)))


class HumanGate(RuntimeError):
    pass


def _validate_idempotent_continuation(item: Continuation) -> None:
    expected = "validar continuidade idempotente"
    if item.project != "AI Control Plane":
        raise HumanGate("automation_action_project_not_allowed")
    if str(item.todo.get("next_action") or "").strip().casefold() != expected:
        raise HumanGate("automation_action_payload_mismatch")
    if not str(item.todo.get("external_id") or "").startswith("desktop-24x7-e2e-"):
        raise HumanGate("automation_action_external_id_not_allowed")


ACTION_REGISTRY = {
    "ai_control_plane.validate_idempotent_continuation.v1": _validate_idempotent_continuation,
}


def execute(item: Continuation) -> None:
    """Executa somente ações tipadas e explicitamente registradas."""
    action = str(item.todo.get("automation_action") or "").strip()
    if not action:
        raise HumanGate("automation_action_missing")
    handler = ACTION_REGISTRY.get(action)
    if handler is None:
        raise HumanGate(f"automation_action_not_registered:{action}")
    handler(item)


def process_batch(queue: Queue, *, limit: int = 10, lease_seconds: int = 120,
                  max_attempts: int = 3, backoff_seconds: int = 30) -> dict[str, int]:
    counts = {"reserved": 0, "completed": 0, "human_gate": 0, "retry": 0, "dlq": 0}
    for item in queue.reserve_continuations(limit, lease_seconds):
        counts["reserved"] += 1
        try:
            execute(item)
            if not queue.complete_continuation(item.request_id):
                raise RuntimeError("completion_transition_rejected")
            counts["completed"] += 1
        except HumanGate as exc:
            queue.gate_continuation(item.request_id, str(exc))
            counts["human_gate"] += 1
        except Exception as exc:
            state = queue.fail_continuation(item.request_id, str(exc), max_attempts, backoff_seconds)
            counts["dlq" if state == "DLQ" else "retry"] += 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Worker contínuo de continuações")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=5)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--lease-seconds", type=int, default=120)
    parser.add_argument("--worker-id", default=os.environ.get("WORKER_ID", "continuation-worker-pc24x7"))
    args = parser.parse_args()
    if not 1 <= args.limit <= 100 or not 1 <= args.lease_seconds <= 3600:
        raise SystemExit("invalid worker limits")
    queue = PostgresContinuationQueue(os.environ.get("DATABASE_URL", ""))
    while True:
        result = process_batch(queue, limit=args.limit, lease_seconds=args.lease_seconds)
        queue.heartbeat(args.worker_id, result)
        print(json.dumps(result, sort_keys=True), flush=True)
        if args.once:
            return 0
        time.sleep(max(1, min(args.interval_seconds, 300)))


if __name__ == "__main__":
    raise SystemExit(main())

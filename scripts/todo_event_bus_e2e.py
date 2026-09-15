#!/usr/bin/env python3
"""E2E isolado do núcleo TodoEvent: positivo, replay, retry/DLQ e lease."""

from __future__ import annotations

import tempfile
from pathlib import Path

from todo_event_bus import (
    DurableSQLiteQueue,
    SQLiteTodoSink,
    TodoEvent,
    make_idempotency_key,
    process_batch,
    utc_now_iso,
)


def build(event_id: str, status: str, key: str) -> TodoEvent:
    return TodoEvent.from_dict(
        {
            "schema_version": "1.0",
            "event_id": event_id,
            "event_type": "todo.updated",
            "occurred_at": utc_now_iso(),
            "correlation_id": "corr-e2e-todo-async-v1",
            "idempotency_key": key,
            "project": "Geral",
            "producer": "todo-event-bus-e2e",
            "todo": {
                "title": "Validar barramento assíncrono de TODOs",
                "type": "Automação",
                "external_id": "todo-global-async-v1",
                "status": status,
                "priority": "P1",
            },
        }
    )


class AlwaysFail:
    def upsert(self, event: TodoEvent) -> str:
        raise RuntimeError("falha e2e controlada")


def main() -> int:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        queue = DurableSQLiteQueue(root / "queue.sqlite", max_attempts=3)
        sink = SQLiteTodoSink(root / "todos.sqlite")
        key = make_idempotency_key("Geral", "Automação", "todo-global-async-v1")

        first = build("evt-e2e-0001", "PENDENTE", key)
        assert queue.enqueue(first, now=100.0) is True
        assert queue.enqueue(first, now=100.0) is False
        assert process_batch(queue, sink, now=100.0, backoff_seconds=0)["processed"] == 1

        second = build("evt-e2e-0002", "EM ANDAMENTO", key)
        assert queue.enqueue(second, now=101.0) is True
        assert process_batch(queue, sink, now=101.0, backoff_seconds=0)["processed"] == 1
        persisted = sink.get(key)
        assert persisted is not None
        assert sink.count() == 1
        assert persisted["status"] == "EM ANDAMENTO"
        assert persisted["last_event_id"] == "evt-e2e-0002"
        assert persisted["update_count"] == 2

        failing = build("evt-e2e-0003", "PENDENTE", make_idempotency_key("Geral", "Teste", "dlq"))
        assert queue.enqueue(failing, now=110.0) is True
        for ts in (110.0, 111.0, 112.0):
            process_batch(queue, AlwaysFail(), now=ts, backoff_seconds=0)
        assert queue.state("evt-e2e-0003")["state"] == "DLQ"
        assert queue.state("evt-e2e-0003")["attempts"] == 3

        leased = build("evt-e2e-0004", "PENDENTE", make_idempotency_key("Geral", "Teste", "lease"))
        assert queue.enqueue(leased, now=120.0) is True
        assert len(queue.reserve(limit=1, lease_seconds=5, now=120.0)) == 1
        assert queue.recover_expired_leases(now=126.0) == 1
        assert queue.state("evt-e2e-0004")["state"] == "PENDING"

        print(
            "TODO_EVENT_E2E_OK "
            f"correlation_id=corr-e2e-todo-async-v1 todos={sink.count()} "
            f"processed={queue.counts()['PROCESSED']} dlq={queue.counts()['DLQ']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

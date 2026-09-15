from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.todo_event_bus import (
    DurableSQLiteQueue,
    SQLiteTodoSink,
    TodoEvent,
    make_idempotency_key,
    process_batch,
    utc_now_iso,
    validate_event_dict,
)


def event(event_id: str, *, status: str = "PENDENTE", key: str | None = None) -> TodoEvent:
    idem = key or make_idempotency_key("Geral", "Automação", "todo-global-async-v1")
    return TodoEvent.from_dict(
        {
            "schema_version": "1.0",
            "event_id": event_id,
            "event_type": "todo.updated",
            "occurred_at": utc_now_iso(),
            "correlation_id": "corr-test-0001",
            "idempotency_key": idem,
            "project": "Geral",
            "producer": "unittest",
            "todo": {
                "title": "TODO assíncrono universal",
                "type": "Automação",
                "external_id": "todo-global-async-v1",
                "status": status,
                "priority": "P1",
            },
        }
    )


def raw_event(event_id: str, status: str) -> dict:
    return {
        "schema_version": "1.0",
        "event_id": event_id,
        "event_type": "todo.updated",
        "occurred_at": utc_now_iso(),
        "correlation_id": "corr-test-0001",
        "idempotency_key": make_idempotency_key("Geral", "Automação", "todo-global-async-v1"),
        "project": "Geral",
        "producer": "unittest",
        "todo": {
            "title": "TODO assíncrono universal",
            "type": "Automação",
            "external_id": "todo-global-async-v1",
            "status": status,
            "priority": "P1",
        },
    }


class FailingSink:
    def upsert(self, event: TodoEvent) -> str:
        raise RuntimeError("falha controlada sem segredo")


class TodoEventBusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.queue = DurableSQLiteQueue(root / "queue.sqlite", max_attempts=3)
        self.sink = SQLiteTodoSink(root / "todos.sqlite")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_rejects_invalid_contract(self) -> None:
        with self.assertRaises(ValueError):
            validate_event_dict({"schema_version": "1.0"})

    def test_rejects_completed_without_completion_evidence(self) -> None:
        payload = raw_event("evt-00000006", "CONCLUÍDO")
        with self.assertRaisesRegex(ValueError, "completion_criteria"):
            validate_event_dict(payload)
        payload["todo"]["completion_criteria"] = "Critério objetivo atendido"
        with self.assertRaisesRegex(ValueError, "evidence"):
            validate_event_dict(payload)
        payload["todo"]["evidence"] = "Leitura independente confirmou o estado"
        validate_event_dict(payload)

    def test_rejects_blocked_without_cause_and_next_action(self) -> None:
        payload = raw_event("evt-00000007", "BLOQUEADO")
        with self.assertRaisesRegex(ValueError, "blocker"):
            validate_event_dict(payload)
        payload["todo"]["blocker"] = "Credencial externa indisponível"
        with self.assertRaisesRegex(ValueError, "next_action"):
            validate_event_dict(payload)
        payload["todo"]["next_action"] = "Configurar credencial autorizada e revalidar"
        validate_event_dict(payload)

    def test_same_event_id_is_enqueued_once(self) -> None:
        current = event("evt-00000001")
        self.assertTrue(self.queue.enqueue(current, now=100.0))
        self.assertFalse(self.queue.enqueue(current, now=100.0))
        self.assertEqual(self.queue.counts()["PENDING"], 1)

    def test_same_logical_todo_is_upserted_without_duplicate(self) -> None:
        key = make_idempotency_key("Geral", "Automação", "todo-global-async-v1")
        self.queue.enqueue(event("evt-00000001", key=key), now=100.0)
        self.assertEqual(process_batch(self.queue, self.sink, now=100.0, backoff_seconds=0)["processed"], 1)
        self.queue.enqueue(event("evt-00000002", status="EM ANDAMENTO", key=key), now=101.0)
        self.assertEqual(process_batch(self.queue, self.sink, now=101.0, backoff_seconds=0)["processed"], 1)
        row = self.sink.get(key)
        self.assertIsNotNone(row)
        self.assertEqual(self.sink.count(), 1)
        self.assertEqual(row["status"], "EM ANDAMENTO")
        self.assertEqual(row["update_count"], 2)
        self.assertEqual(row["last_event_id"], "evt-00000002")

    def test_retry_goes_to_dlq_on_third_attempt(self) -> None:
        self.queue.enqueue(event("evt-00000003"), now=100.0)
        for ts in (100.0, 101.0, 102.0):
            process_batch(self.queue, FailingSink(), now=ts, backoff_seconds=0)
        state = self.queue.state("evt-00000003")
        self.assertEqual(state["state"], "DLQ")
        self.assertEqual(state["attempts"], 3)
        self.assertIn("falha controlada", state["last_error"])

    def test_expired_lease_recovers_event(self) -> None:
        self.queue.enqueue(event("evt-00000004"), now=100.0)
        reserved = self.queue.reserve(limit=1, lease_seconds=5, now=100.0)
        self.assertEqual(len(reserved), 1)
        self.assertEqual(self.queue.state("evt-00000004")["state"], "PROCESSING")
        recovered = self.queue.recover_expired_leases(now=106.0)
        self.assertEqual(recovered, 1)
        self.assertEqual(self.queue.state("evt-00000004")["state"], "PENDING")

    def test_ack_requires_active_reservation(self) -> None:
        self.queue.enqueue(event("evt-00000005"), now=100.0)
        with self.assertRaises(ValueError):
            self.queue.ack("evt-00000005", now=100.0)


if __name__ == "__main__":
    unittest.main()

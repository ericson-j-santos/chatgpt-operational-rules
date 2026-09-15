from __future__ import annotations

import contextlib
import unittest
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from scripts.todo_event_bus import TodoEvent, make_idempotency_key, utc_now_iso
from services.todo_gateway.gateway import create_app
from services.todo_gateway.notion_sink import (
    NotionTodoSink,
    PermanentSinkError,
    TransientSinkError,
    VerificationError,
)
from services.todo_gateway.worker import process_once


def sample_event(*, event_id: str = "evt-gateway-0001", status: str = "EM ANDAMENTO") -> TodoEvent:
    todo = {
        "title": "TODO assíncrono universal",
        "type": "Automação",
        "external_id": "todo-global-async-v1",
        "status": status,
        "priority": "P1",
        "source": "ChatGPT",
        "next_action": "Executar E2E externo",
    }
    if status == "BLOQUEADO":
        todo["blocker"] = "bloqueio controlado"
    if status == "CONCLUÍDO":
        todo["completion_criteria"] = "efeito verificado"
        todo["evidence"] = "leitura independente verde"
    return TodoEvent.from_dict(
        {
            "schema_version": "1.0",
            "event_id": event_id,
            "event_type": "todo.updated",
            "occurred_at": utc_now_iso(),
            "correlation_id": "corr-gateway-0001",
            "idempotency_key": make_idempotency_key("Geral", "Automação", "todo-global-async-v1"),
            "project": "Geral",
            "producer": "unittest",
            "todo": todo,
        }
    )


class FakeGatewayQueue:
    def __init__(self) -> None:
        self.ids: set[str] = set()
        self.fail = False

    def ready(self) -> bool:
        if self.fail:
            raise RuntimeError("db unavailable")
        return True

    def enqueue(self, event: TodoEvent) -> bool:
        if self.fail:
            raise RuntimeError("db unavailable")
        inserted = event.event_id not in self.ids
        self.ids.add(event.event_id)
        return inserted


class GatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.queue = FakeGatewayQueue()
        self.client = TestClient(create_app(self.queue, "test-secret"))

    def test_accepts_and_identifies_replay(self) -> None:
        payload = sample_event().to_dict()
        headers = {"Authorization": "Bearer test-secret"}
        first = self.client.post("/v1/events", json=payload, headers=headers)
        replay = self.client.post("/v1/events", json=payload, headers=headers)
        self.assertEqual(first.status_code, 202)
        self.assertFalse(first.json()["duplicate"])
        self.assertEqual(replay.status_code, 202)
        self.assertTrue(replay.json()["duplicate"])

    def test_rejects_unauthorized(self) -> None:
        response = self.client.post("/v1/events", json=sample_event().to_dict())
        self.assertEqual(response.status_code, 401)

    def test_fails_closed_when_queue_is_unavailable(self) -> None:
        self.queue.fail = True
        response = self.client.post(
            "/v1/events",
            json=sample_event().to_dict(),
            headers={"Authorization": "Bearer test-secret"},
        )
        self.assertEqual(response.status_code, 503)

    def test_rejects_completed_without_evidence(self) -> None:
        payload = sample_event().to_dict()
        payload["todo"]["status"] = "CONCLUÍDO"
        response = self.client.post(
            "/v1/events",
            json=payload,
            headers={"Authorization": "Bearer test-secret"},
        )
        self.assertEqual(response.status_code, 422)


def notion_page(page_id: str, event: TodoEvent) -> dict:
    return {
        "id": page_id,
        "properties": {
            "Chave de idempotência": {"rich_text": [{"plain_text": event.idempotency_key}]},
            "Correlation ID": {"rich_text": [{"plain_text": event.correlation_id}]},
            "Status": {"select": {"name": event.todo["status"]}},
        },
    }


class NotionSinkTests(unittest.TestCase):
    def test_create_and_independent_readback(self) -> None:
        event = sample_event()
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path))
            if request.url.path.endswith("/query"):
                return httpx.Response(200, json={"results": []})
            if request.method == "POST" and request.url.path == "/v1/pages":
                return httpx.Response(200, json={"id": "page-1"})
            if request.method == "GET" and request.url.path == "/v1/pages/page-1":
                return httpx.Response(200, json=notion_page("page-1", event))
            return httpx.Response(500)

        client = httpx.Client(base_url="https://api.notion.com", transport=httpx.MockTransport(handler))
        sink = NotionTodoSink("token", "ds-1", client=client)
        self.assertEqual(sink.upsert(event), "page-1")
        self.assertIn(("GET", "/v1/pages/page-1"), calls)

    def test_updates_existing_page(self) -> None:
        event = sample_event()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/query"):
                return httpx.Response(200, json={"results": [{"id": "page-1"}]})
            if request.method == "PATCH":
                return httpx.Response(200, json={"id": "page-1"})
            if request.method == "GET":
                return httpx.Response(200, json=notion_page("page-1", event))
            return httpx.Response(500)

        sink = NotionTodoSink(
            "token",
            "ds-1",
            client=httpx.Client(base_url="https://api.notion.com", transport=httpx.MockTransport(handler)),
        )
        self.assertEqual(sink.upsert(event), "page-1")

    def test_duplicate_canonical_pages_are_permanent_failure(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"results": [{"id": "a"}, {"id": "b"}]})

        sink = NotionTodoSink(
            "token",
            "ds-1",
            client=httpx.Client(base_url="https://api.notion.com", transport=httpx.MockTransport(handler)),
        )
        with self.assertRaises(PermanentSinkError):
            sink.upsert(sample_event())

    def test_false_success_is_rejected_by_readback(self) -> None:
        event = sample_event()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/query"):
                return httpx.Response(200, json={"results": [{"id": "page-1"}]})
            if request.method == "PATCH":
                return httpx.Response(200, json={"id": "page-1"})
            if request.method == "GET":
                wrong = notion_page("page-1", event)
                wrong["properties"]["Status"]["select"]["name"] = "PENDENTE"
                return httpx.Response(200, json=wrong)
            return httpx.Response(500)

        sink = NotionTodoSink(
            "token",
            "ds-1",
            client=httpx.Client(base_url="https://api.notion.com", transport=httpx.MockTransport(handler)),
        )
        with self.assertRaises(VerificationError):
            sink.upsert(event)

    def test_503_is_transient(self) -> None:
        sink = NotionTodoSink(
            "token",
            "ds-1",
            client=httpx.Client(
                base_url="https://api.notion.com",
                transport=httpx.MockTransport(lambda request: httpx.Response(503)),
            ),
        )
        with self.assertRaises(TransientSinkError):
            sink.upsert(sample_event())


class FakeWorkerQueue:
    def __init__(self, item) -> None:
        self.item = item
        self.acked: list[str] = []
        self.failed: list[tuple] = []
        self.locked: list[str] = []

    def reserve(self, **kwargs):
        return [self.item]

    @contextlib.contextmanager
    def idempotency_lock(self, key: str):
        self.locked.append(key)
        yield

    def ack(self, event_id: str) -> bool:
        self.acked.append(event_id)
        return True

    def fail(self, event_id: str, error: str, **kwargs) -> str:
        self.failed.append((event_id, error, kwargs))
        return "DLQ" if kwargs.get("max_attempts") == 1 else "PENDING"


class WorkerTests(unittest.TestCase):
    def test_success_uses_idempotency_lock_and_ack(self) -> None:
        event = sample_event()
        queue = FakeWorkerQueue(SimpleNamespace(event=event, attempts=1))
        sink = SimpleNamespace(upsert=lambda e: "page-1")
        result = process_once(queue, sink)
        self.assertEqual(result["processed"], 1)
        self.assertEqual(queue.locked, [event.idempotency_key])
        self.assertEqual(queue.acked, [event.event_id])

    def test_permanent_failure_goes_to_dlq(self) -> None:
        event = sample_event()
        queue = FakeWorkerQueue(SimpleNamespace(event=event, attempts=1))

        class Sink:
            def upsert(self, e):
                raise PermanentSinkError("duplicate canonical TODOs")

        result = process_once(queue, Sink())
        self.assertEqual(result["dlq"], 1)
        self.assertEqual(queue.failed[0][2]["max_attempts"], 1)

    def test_transient_failure_is_retried(self) -> None:
        event = sample_event()
        queue = FakeWorkerQueue(SimpleNamespace(event=event, attempts=1))

        class Sink:
            def upsert(self, e):
                raise TransientSinkError("Notion 503")

        result = process_once(queue, Sink())
        self.assertEqual(result["retried"], 1)
        self.assertEqual(queue.failed[0][2]["max_attempts"], 3)


if __name__ == "__main__":
    unittest.main()

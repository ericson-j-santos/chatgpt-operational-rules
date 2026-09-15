#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from scripts.todo_event_bus import TodoEvent, make_idempotency_key, utc_now_iso
from services.todo_gateway.gateway import create_app
from services.todo_gateway.notion_sink import NotionTodoSink
from services.todo_gateway.worker import process_once


class MemoryQueue:
    def __init__(self) -> None:
        self.events: dict[str, TodoEvent] = {}
        self.pending: list[str] = []
        self.attempts: dict[str, int] = {}
        self.processed: set[str] = set()

    def ready(self) -> bool:
        return True

    def enqueue(self, event: TodoEvent) -> bool:
        if event.event_id in self.events:
            return False
        self.events[event.event_id] = event
        self.pending.append(event.event_id)
        self.attempts[event.event_id] = 0
        return True

    def reserve(self, **kwargs):
        if not self.pending:
            return []
        event_id = self.pending.pop(0)
        self.attempts[event_id] += 1
        return [SimpleNamespace(event=self.events[event_id], attempts=self.attempts[event_id])]

    @contextlib.contextmanager
    def idempotency_lock(self, key: str):
        yield

    def ack(self, event_id: str) -> bool:
        self.processed.add(event_id)
        return True

    def fail(self, event_id: str, error: str, **kwargs) -> str:
        self.pending.append(event_id)
        return "PENDING"


class NotionMemory:
    def __init__(self) -> None:
        self.page_id = "page-e2e-1"
        self.properties = None
        self.creates = 0
        self.updates = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/query"):
            results = [] if self.properties is None else [{"id": self.page_id}]
            return httpx.Response(200, json={"results": results})
        if request.method == "POST" and request.url.path == "/v1/pages":
            body = json.loads(request.content)
            self.properties = body["properties"]
            self.creates += 1
            return httpx.Response(200, json={"id": self.page_id})
        if request.method == "PATCH" and request.url.path == f"/v1/pages/{self.page_id}":
            body = json.loads(request.content)
            self.properties = body["properties"]
            self.updates += 1
            return httpx.Response(200, json={"id": self.page_id})
        if request.method == "GET" and request.url.path == f"/v1/pages/{self.page_id}":
            props = json.loads(json.dumps(self.properties))
            for value in props.values():
                if "rich_text" in value:
                    for chunk in value["rich_text"]:
                        content = chunk.get("text", {}).get("content", "")
                        chunk["plain_text"] = content
            return httpx.Response(200, json={"id": self.page_id, "properties": props})
        return httpx.Response(500)


def payload(event_id: str, status: str) -> dict:
    todo = {
        "title": "TODO Gateway E2E",
        "type": "Automação",
        "external_id": "todo-gateway-e2e",
        "status": status,
        "priority": "P1",
        "source": "ChatGPT",
        "next_action": "validar projeções",
    }
    if status == "BLOQUEADO":
        todo["blocker"] = "bloqueio e2e controlado"
    return {
        "schema_version": "1.0",
        "event_id": event_id,
        "event_type": "todo.updated",
        "occurred_at": utc_now_iso(),
        "correlation_id": "corr-gateway-e2e-0001",
        "idempotency_key": make_idempotency_key("Geral", "Automação", "todo-gateway-e2e"),
        "project": "Geral",
        "producer": "e2e",
        "todo": todo,
    }


def main() -> int:
    queue = MemoryQueue()
    app = create_app(queue, "e2e-secret")
    client = TestClient(app)
    notion = NotionMemory()
    http = httpx.Client(
        base_url="https://api.notion.com",
        transport=httpx.MockTransport(notion.handler),
    )
    sink = NotionTodoSink("token", "ds-e2e", client=http)
    headers = {"Authorization": "Bearer e2e-secret"}

    first_payload = payload("evt-gateway-e2e-0001", "EM ANDAMENTO")
    first = client.post("/v1/events", json=first_payload, headers=headers)
    replay = client.post("/v1/events", json=first_payload, headers=headers)
    assert first.status_code == 202 and first.json()["duplicate"] is False
    assert replay.status_code == 202 and replay.json()["duplicate"] is True
    assert process_once(queue, sink)["processed"] == 1
    assert notion.creates == 1 and notion.updates == 0

    second = client.post(
        "/v1/events",
        json=payload("evt-gateway-e2e-0002", "BLOQUEADO"),
        headers=headers,
    )
    assert second.status_code == 202
    assert process_once(queue, sink)["processed"] == 1
    assert notion.creates == 1 and notion.updates == 1

    invalid = payload("evt-gateway-e2e-0003", "EM ANDAMENTO")
    invalid["todo"]["status"] = "CONCLUÍDO"
    rejected = client.post("/v1/events", json=invalid, headers=headers)
    assert rejected.status_code == 422
    assert len(queue.events) == 2
    assert len(queue.processed) == 2

    print(
        "TODO_GATEWAY_E2E_OK "
        "events=2 notion_pages=1 replay_duplicate=true "
        "invalid_completion_rejected=true"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

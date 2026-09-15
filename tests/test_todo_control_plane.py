from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from services.todo_gateway.gateway import create_app

KEY = "a" * 64
TERMINAL_KEY = "b" * 64
HEADERS = {"Authorization": "Bearer control-secret"}


class FakeControlQueue:
    def __init__(self) -> None:
        self.requests: dict[tuple[str, str], dict] = {}
        self.items = [
            {
                "event_id": "evt-control-0001",
                "idempotency_key": KEY,
                "correlation_id": "corr-control-0001",
                "project": "AI Control Plane",
                "producer": "unittest",
                "queue_state": "PROCESSED",
                "attempts": 1,
                "last_error": None,
                "created_at": "2026-09-15T00:00:00+00:00",
                "updated_at": "2026-09-15T00:00:00+00:00",
                "todo": {
                    "title": "Centralizar solicitações",
                    "type": "Implementação",
                    "status": "EM ANDAMENTO",
                    "priority": "P0",
                    "next_action": "Executar próximo incremento",
                },
            },
            {
                "event_id": "evt-control-0002",
                "idempotency_key": TERMINAL_KEY,
                "correlation_id": "corr-control-0002",
                "project": "Outro",
                "producer": "unittest",
                "queue_state": "PROCESSED",
                "attempts": 1,
                "last_error": None,
                "created_at": "2026-09-15T00:00:01+00:00",
                "updated_at": "2026-09-15T00:00:01+00:00",
                "todo": {
                    "title": "Item concluído",
                    "type": "Validação",
                    "status": "CONCLUÍDO",
                    "completion_criteria": "validado",
                    "evidence": "evidência",
                },
            },
        ]

    def ready(self) -> bool:
        return True

    def enqueue(self, event) -> bool:
        return True

    def list_latest_todos(self, limit: int = 100):
        return self.items[:limit]

    def get_latest_todo(self, idempotency_key: str):
        return next(
            (item for item in self.items if item["idempotency_key"] == idempotency_key),
            None,
        )

    def request_continuation(self, idempotency_key: str, correlation_id: str):
        item = self.get_latest_todo(idempotency_key)
        if item is None:
            return None
        status = item["todo"]["status"]
        if status in {"CONCLUÍDO", "CANCELADO"}:
            raise ValueError(f"terminal TODO cannot be continued: {status}")
        request_key = (idempotency_key, item["event_id"])
        duplicate = request_key in self.requests
        if not duplicate:
            self.requests[request_key] = {
                "request_id": "cont-control-0001",
                "idempotency_key": idempotency_key,
                "basis_event_id": item["event_id"],
                "correlation_id": correlation_id,
                "project": item["project"],
                "state": "PENDING",
                "attempts": 0,
                "last_error": None,
                "created_at": "2026-09-15T00:01:00+00:00",
                "updated_at": "2026-09-15T00:01:00+00:00",
            }
        result = dict(self.requests[request_key])
        result["duplicate"] = duplicate
        return result

    def list_continuation_requests(self, limit: int = 100):
        return list(self.requests.values())[:limit]


class ControlPlaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.queue = FakeControlQueue()
        self.client = TestClient(create_app(self.queue, "control-secret"))

    def test_list_todos_requires_authentication(self) -> None:
        response = self.client.get("/v1/todos")
        self.assertEqual(response.status_code, 401)

    def test_list_todos_filters_status_and_project(self) -> None:
        response = self.client.get(
            "/v1/todos",
            params={"status": "EM ANDAMENTO", "project": "ai control plane"},
            headers=HEADERS,
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["items"][0]["idempotency_key"], KEY)

    def test_invalid_status_is_rejected(self) -> None:
        response = self.client.get(
            "/v1/todos",
            params={"status": "INVALID"},
            headers=HEADERS,
        )
        self.assertEqual(response.status_code, 422)

    def test_continuation_request_is_idempotent_for_same_basis_event(self) -> None:
        first = self.client.post(
            f"/v1/todos/{KEY}/continue",
            json={"correlation_id": "corr-continue-0001"},
            headers=HEADERS,
        )
        replay = self.client.post(
            f"/v1/todos/{KEY}/continue",
            json={"correlation_id": "corr-continue-0002"},
            headers=HEADERS,
        )
        self.assertEqual(first.status_code, 202)
        self.assertFalse(first.json()["duplicate"])
        self.assertEqual(replay.status_code, 202)
        self.assertTrue(replay.json()["duplicate"])
        self.assertEqual(first.json()["request_id"], replay.json()["request_id"])

    def test_terminal_todo_cannot_be_continued(self) -> None:
        response = self.client.post(
            f"/v1/todos/{TERMINAL_KEY}/continue",
            headers=HEADERS,
        )
        self.assertEqual(response.status_code, 409)

    def test_missing_todo_returns_404(self) -> None:
        response = self.client.post(
            f"/v1/todos/{'c' * 64}/continue",
            headers=HEADERS,
        )
        self.assertEqual(response.status_code, 404)

    def test_continuation_queue_is_readable(self) -> None:
        self.client.post(f"/v1/todos/{KEY}/continue", headers=HEADERS)
        response = self.client.get("/v1/continuations", headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)


if __name__ == "__main__":
    unittest.main()

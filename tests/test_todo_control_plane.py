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
        self.fronts: dict[str, dict] = {}
        self.front_history: dict[str, list[dict]] = {}
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


    def record_operational_front(self, front_id: str, snapshot: dict, correlation_id: str):
        required = ("title", "state", "source_of_truth", "source_ref", "source_event_id", "source_updated_at")
        missing = [name for name in required if not str(snapshot.get(name) or "").strip()]
        if missing:
            raise ValueError("missing front fields: " + ", ".join(missing))
        if not isinstance(snapshot.get("blockers", []), list):
            raise ValueError("blockers must be an array")
        history = self.front_history.setdefault(front_id, [])
        event_key = (snapshot["source_of_truth"], snapshot["source_event_id"])
        previous = next((item for item in history if item["event_key"] == event_key), None)
        current = self.fronts.get(front_id)
        if previous is not None:
            result = dict(current)
            result.update({"outcome": "DUPLICATE", "duplicate": True})
            return result
        event_id = f"front-event-{len(history) + 1}"
        history.append({"event_key": event_key, "event_id": event_id, "snapshot": dict(snapshot)})
        incoming = snapshot["source_updated_at"]
        if current is not None and incoming < current["source_updated_at"]:
            result = dict(current)
            result.update({"outcome": "STALE", "duplicate": False})
            return result
        if current is not None and incoming == current["source_updated_at"]:
            result = dict(current)
            result.update({"outcome": "CONFLICT", "duplicate": False})
            return result
        result = {
            "front_id": front_id,
            "title": snapshot["title"],
            "state": snapshot["state"],
            "source_of_truth": snapshot["source_of_truth"],
            "source_ref": snapshot["source_ref"],
            "source_updated_at": incoming,
            "current_event_id": event_id,
            "correlation_id": correlation_id,
            "last_sha": snapshot.get("last_sha"),
            "last_pr": snapshot.get("last_pr"),
            "last_issue": snapshot.get("last_issue"),
            "last_run": snapshot.get("last_run"),
            "next_step": snapshot.get("next_step"),
            "blockers": list(snapshot.get("blockers", [])),
            "summary": snapshot.get("summary"),
        }
        self.fronts[front_id] = dict(result)
        result.update({"outcome": "APPLIED", "duplicate": False})
        return result

    def list_operational_fronts(self, limit: int = 100):
        return list(self.fronts.values())[:limit]

    def list_operational_front_history(self, front_id: str, limit: int = 100):
        return list(self.front_history.get(front_id, []))[:limit]


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


    def _front_payload(self, **overrides):
        payload = {
            "correlation_id": "corr-front-0001",
            "title": "ReqSys Service Management",
            "state": "EM ANDAMENTO",
            "source_of_truth": "github",
            "source_ref": "ericson-j-santos/reqsys-v2-enterprise-real#1789",
            "source_event_id": "issue-1789-update-1",
            "source_updated_at": "2026-09-20T14:00:00+00:00",
            "last_sha": "a" * 40,
            "last_pr": "#1819",
            "last_issue": "#1789",
            "last_run": "35510000000",
            "next_step": "Fechar ciclo CHANGE",
            "blockers": [],
            "summary": "RSM-07 em andamento",
        }
        payload.update(overrides)
        return payload

    def test_front_registry_requires_authentication(self) -> None:
        response = self.client.get("/v1/fronts")
        self.assertEqual(response.status_code, 401)

    def test_front_snapshot_is_persisted_and_readable(self) -> None:
        created = self.client.post(
            "/v1/fronts/reqsys-change",
            json=self._front_payload(),
            headers=HEADERS,
        )
        self.assertEqual(created.status_code, 202)
        self.assertEqual(created.json()["outcome"], "APPLIED")
        listed = self.client.get("/v1/fronts", headers=HEADERS)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["count"], 1)
        self.assertEqual(listed.json()["items"][0]["source_of_truth"], "github")

    def test_front_replay_is_idempotent(self) -> None:
        first = self.client.post("/v1/fronts/reqsys-change", json=self._front_payload(), headers=HEADERS)
        replay = self.client.post("/v1/fronts/reqsys-change", json=self._front_payload(), headers=HEADERS)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(replay.status_code, 202)
        self.assertTrue(replay.json()["duplicate"])
        self.assertEqual(replay.json()["outcome"], "DUPLICATE")

    def test_stale_front_event_does_not_replace_current_state(self) -> None:
        self.client.post("/v1/fronts/reqsys-change", json=self._front_payload(), headers=HEADERS)
        stale = self.client.post(
            "/v1/fronts/reqsys-change",
            json=self._front_payload(
                source_event_id="issue-1789-old",
                source_updated_at="2026-09-19T14:00:00+00:00",
                state="PENDENTE",
            ),
            headers=HEADERS,
        )
        self.assertEqual(stale.status_code, 202)
        self.assertEqual(stale.json()["outcome"], "STALE")
        current = self.client.get("/v1/fronts", headers=HEADERS).json()["items"][0]
        self.assertEqual(current["state"], "EM ANDAMENTO")

    def test_same_timestamp_conflict_fails_closed(self) -> None:
        self.client.post("/v1/fronts/reqsys-change", json=self._front_payload(), headers=HEADERS)
        conflict = self.client.post(
            "/v1/fronts/reqsys-change",
            json=self._front_payload(source_event_id="issue-1789-conflict", state="CONCLUÍDO"),
            headers=HEADERS,
        )
        self.assertEqual(conflict.status_code, 409)
        current = self.client.get("/v1/fronts", headers=HEADERS).json()["items"][0]
        self.assertEqual(current["state"], "EM ANDAMENTO")


if __name__ == "__main__":
    unittest.main()

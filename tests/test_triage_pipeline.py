"""Pipeline integration: real HTTP/PostgreSQL/worker; GitHub and Notion are simulated.

No test here proves live Notion access. Missing disposable PostgreSQL fails rather
than skipping the suite. Never use this fixture against a persistent database.
"""
from __future__ import annotations

import copy
import json
import os
import socket
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
import uvicorn

from services.todo_gateway.gateway import create_app
from services.todo_gateway.repository import PostgresQueueRepository
from services.todo_gateway.triage_reservation import initialize_schema, scope_for
from services.todo_gateway.triage_pipeline import (
    TriageProjectionSink, TriagePublisher, event_id_for, install_pipeline, queue_key,
    publisher_from_env, projection_meta,
)
from services.todo_gateway.worker import process_once
from scripts.todo_event_bus import TodoEvent

SOURCE = "2026-10-09T19:56:00.244Z"
AFTER = "2026-10-09T19:57:00.244Z"
TOKEN = "synthetic-gateway-token"


def rich(value):
    return {"type": "text", "text": {"content": value}}


class ExternalServices:
    """Independent persisted page model; NOT a real Notion service."""
    def __init__(self, key, correlation):
        self.page_id, self.data_source = str(uuid4()), str(uuid4())
        self.page = {"id": self.page_id, "last_edited_time": SOURCE, "archived": False,
                     "parent": {"type": "data_source_id", "data_source_id": self.data_source},
                     "properties": {
                         "Título": {"title": [rich("Preservar triagem existente")]},
                         "Chave de idempotência": {"rich_text": [rich(key)]},
                         "Correlation ID": {"rich_text": [rich(correlation)]},
                         "Projeto": {"rich_text": [rich("Portfólio de teste")]},
                         "Tipo": {"select": {"name": "Acompanhamento"}},
                         "Status": {"select": {"name": "EM ANDAMENTO"}},
                         "Evidência": {"rich_text": [
                             {"type": "text", "text": {"content": "Histórico preservado",
                                                      "link": {"url": "https://example.invalid/evidence"}},
                              "annotations": {"bold": True}},
                         ]},
                         "Bloqueio": {"rich_text": [rich("Não remover")]},
                         "Próxima ação": {"rich_text": [rich("Não alterar")]},
                         "Responsável": {"people": [{"id": str(uuid4())}]},
                     }}
        self.initial = copy.deepcopy(self.page)
        self.calls, self.patches = [], 0
        self.gone = self.duplicate = self.corrupt_write = self.timeout_after_write = False
        self.patch_status = 0
        self.move_head = False
        self.branch_reads = 0
        self.lock = threading.Lock()

    def notion(self, request):
        with self.lock:
            self.calls.append((request.method, request.url.path))
            if request.url.path.endswith("/query"):
                results = [] if self.gone else [{"id": self.page_id}]
                if self.duplicate:
                    results += [{"id": str(uuid4())}]
                return httpx.Response(200, json={"results": results, "has_more": False})
            if request.method == "GET" and request.url.path == "/v1/pages/" + self.page_id:
                return httpx.Response(200, json=copy.deepcopy(self.page))
            if request.method == "PATCH" and request.url.path == "/v1/pages/" + self.page_id:
                if self.patch_status:
                    return httpx.Response(self.patch_status, headers={"Retry-After": "120"})
                body = json.loads(request.content)
                if set(body) != {"properties"} or set(body["properties"]) != {"Evidência"}:
                    return httpx.Response(400, json={"error": "unexpected write"})
                self.patches += 1
                self.page["properties"].update(copy.deepcopy(body["properties"]))
                self.page["last_edited_time"] = AFTER
                if self.corrupt_write:
                    self.page["properties"]["Evidência"] = {"rich_text": [rich("incorrect")]}
                if self.timeout_after_write:
                    self.timeout_after_write = False
                    return httpx.Response(503)
                return httpx.Response(200, json={"id": self.page_id})
            return httpx.Response(405)  # A POST /pages would be an error, never a replacement.

    def github(self, request):
        if "/branches/" in request.url.path:
            self.branch_reads += 1
            sha = "b" * 40 if self.move_head and self.branch_reads > 1 else "a" * 40
            return httpx.Response(200, json={"name": "main", "commit": {"sha": sha}})
        name = request.url.path.removeprefix("/repos/")
        return httpx.Response(200, json={"full_name": name, "default_branch": "main",
                                        "archived": False, "disabled": False, "fork": False})


class PipelineE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dsn = os.environ.get("TRIAGE_TEST_DSN", "")
        cfg = conninfo_to_dict(cls.dsn) if cls.dsn else {}
        if (os.environ.get("GITHUB_ACTIONS") != "true" or cfg.get("host") != "127.0.0.1"
                or cfg.get("dbname") != "todo_reservation_test"):
            raise RuntimeError("pipeline E2E requires the dedicated disposable CI database")
        with psycopg.connect(cls.dsn) as conn:
            conn.execute((Path(__file__).resolve().parents[1] / "sql/todo_bus_postgres.sql").read_text())
        initialize_schema(PostgresQueueRepository(cls.dsn))

    def setUp(self):
        self.key = "portfolio-github-test-" + uuid4().hex
        self.correlation = "corr-" + uuid4().hex
        self.remote = ExternalServices(self.key, self.correlation)
        self.queue = PostgresQueueRepository(self.dsn)
        self.notion_client = httpx.Client(base_url="https://api.notion.com",
                                         transport=httpx.MockTransport(self.remote.notion))
        self.github_client = httpx.Client(base_url="https://api.github.com",
                                         transport=httpx.MockTransport(self.remote.github))
        self.sink = TriageProjectionSink("synthetic-notion-token", self.remote.data_source,
                                        client=self.notion_client, queue=self.queue)
        self.publisher = TriagePublisher(self.queue, self.sink, self.github_client,
                                         self.remote.page_id, "fixture")
        self.body = {"todo_key": self.key, "request_id": "request-" + uuid4().hex,
                     "correlation_id": self.correlation, "expected_version": 0,
                     "source_version": SOURCE, "inventory": ["fixture/repository-01"], "known": []}
        app = create_app(self.queue, TOKEN)
        install_pipeline(app, self.queue, TOKEN, self.publisher)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(128)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.sock]}, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 5
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        if not self.server.started:
            raise RuntimeError("pipeline HTTP server did not start")

    def tearDown(self):
        self.server.should_exit = True
        self.thread.join(5)
        self.sock.close()
        self.notion_client.close()
        self.github_client.close()
        if self.thread.is_alive():
            raise RuntimeError("pipeline HTTP cleanup failed")
        with psycopg.connect(self.dsn) as conn:
            conn.execute("DELETE FROM todo_bus.queue_event_history WHERE correlation_id=%s", (self.correlation,))
            conn.execute("DELETE FROM todo_bus.queue_events WHERE idempotency_key=%s", (queue_key(self.key),))
            conn.execute("DELETE FROM todo_bus.triage_reservations WHERE scope_key=%s", (scope_for(self.key),))

    def post(self, body=None, token=TOKEN, action="publish"):
        return httpx.post(self.url + "/v1/triage/" + action, json=body or self.body,
                          headers={"Authorization": "Bearer " + token}, timeout=10)

    def independent(self):
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute("SELECT checkpoint FROM todo_bus.triage_reservations WHERE scope_key=%s",
                               (scope_for(self.key),)).fetchone()
            events = conn.execute("SELECT event_id,payload,state FROM todo_bus.queue_events WHERE idempotency_key=%s",
                                  (queue_key(self.key),)).fetchall()
        return row[0] if row else None, events

    def ready_retry(self):
        with psycopg.connect(self.dsn) as conn:
            conn.execute("UPDATE todo_bus.queue_events SET available_at=clock_timestamp() WHERE idempotency_key=%s",
                         (queue_key(self.key),))

    def test_full_pipeline_preserves_page_and_replays_once(self):
        submitted = self.post()
        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.assertFalse(submitted.json()["readback_confirmed"])
        state, events = self.independent()
        self.assertEqual(state["version"], 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][2], "PENDING")
        self.assertEqual(events[0][1]["todo"]["triage_projection"]["canonical_key"], self.key)
        self.assertEqual(self.remote.patches, 0)
        for _ in range(2):
            self.assertEqual(self.post().status_code, 200)
        self.assertEqual(process_once(self.queue, self.sink)["processed"], 1)
        confirmed = self.post().json()
        self.assertTrue(confirmed["readback_confirmed"])
        self.assertFalse(confirmed["todo_completed"])
        self.assertEqual(confirmed["external_projection"], "CONFIRMED")
        state, events = self.independent()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][2], "PROCESSED")
        self.assertEqual(state["projection"]["state"], "CONFIRMED")
        self.assertEqual(state["source_version"], AFTER)
        self.assertEqual(self.remote.patches, 1)
        for key, value in self.remote.initial["properties"].items():
            if key != "Evidência":
                self.assertEqual(self.remote.page["properties"][key], value)
        self.assertEqual(self.remote.page["properties"]["Evidência"]["rich_text"][0],
                         self.remote.initial["properties"]["Evidência"]["rich_text"][0])
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.remote.patches, 1)
        self.assertNotIn(("POST", "/v1/pages"), self.remote.calls)
        print("TRIAGE_PIPELINE_OK real_http=true real_postgres=true existing_worker=true "
              "events=1 patches=1 legacy_key_preserved=true live_notion=false")

    def test_queue_failure_rolls_back_checkpoint_and_same_request_recovers(self):
        constraint = "e2e_pipeline_" + uuid4().hex
        with psycopg.connect(self.dsn) as conn:
            conn.execute(sql.SQL("ALTER TABLE todo_bus.queue_events ADD CONSTRAINT {} CHECK (idempotency_key <> {}) NOT VALID")
                         .format(sql.Identifier(constraint), sql.Literal(queue_key(self.key))))
        try:
            response = self.post()
            self.assertEqual(response.status_code, 503)
            state, events = self.independent()
            self.assertEqual(state["version"], 0)
            self.assertEqual(state["records"], {})
            self.assertEqual(events, [])
            self.assertIsNone(state["active"])
        finally:
            with psycopg.connect(self.dsn) as conn:
                conn.execute(sql.SQL("ALTER TABLE todo_bus.queue_events DROP CONSTRAINT {}").format(sql.Identifier(constraint)))
        self.assertEqual(self.post().status_code, 200)
        state, events = self.independent()
        self.assertEqual(state["version"], 1)
        self.assertEqual(len(events), 1)

    def test_response_lost_after_notion_write_does_not_repeat_patch(self):
        self.assertEqual(self.post().status_code, 200)
        self.remote.timeout_after_write = True
        self.assertEqual(process_once(self.queue, self.sink)["retried"], 1)
        self.assertEqual(self.remote.patches, 1)
        self.ready_retry()
        self.assertEqual(process_once(self.queue, self.sink)["processed"], 1)
        self.assertTrue(self.post().json()["readback_confirmed"])
        self.assertEqual(self.remote.patches, 1)

    def test_new_process_resumes_durable_request_without_enqueue(self):
        self.assertEqual(self.post().status_code, 200)
        restarted = TriagePublisher(self.queue, self.sink, self.github_client, self.remote.page_id, "fixture")
        self.assertEqual(restarted.run(self.body)["queue_state"], "PENDING")
        self.assertEqual(len(self.independent()[1]), 1)
        self.assertEqual(process_once(self.queue, self.sink)["processed"], 1)
        self.assertTrue(restarted.run(self.body)["readback_confirmed"])

    def test_same_request_with_different_scope_conflicts(self):
        self.assertEqual(self.post().status_code, 200)
        changed = {**self.body, "inventory": ["fixture/repository-02"]}
        self.assertEqual(self.post(changed).status_code, 409)
        self.assertEqual(len(self.independent()[1]), 1)

    def test_github_head_drift_blocks_and_releases_attempt(self):
        self.remote.move_head = True
        self.assertEqual(self.post().status_code, 409)
        state, events = self.independent()
        self.assertEqual(events, [])
        self.assertEqual(state["version"], 0)
        self.assertIsNone(state["active"])
        self.remote.move_head = False
        self.assertEqual(self.post().status_code, 200)

    def test_stale_source_blocks_before_reserving(self):
        self.remote.page["last_edited_time"] = AFTER
        self.assertEqual(self.post().status_code, 409)
        self.assertEqual(self.independent(), (None, []))

    def test_changed_page_after_enqueue_is_not_overwritten(self):
        self.assertEqual(self.post().status_code, 200)
        self.remote.page["last_edited_time"] = AFTER
        self.remote.page["properties"]["Evidência"] = {"rich_text": [rich("new human evidence")]}
        self.assertEqual(process_once(self.queue, self.sink)["dlq"], 1)
        self.assertEqual(self.remote.patches, 0)
        self.assertFalse(self.post().json()["readback_confirmed"])

    def test_false_success_cannot_ack_or_confirm(self):
        self.assertEqual(self.post().status_code, 200)
        self.remote.corrupt_write = True
        self.assertEqual(process_once(self.queue, self.sink)["retried"], 1)
        self.assertNotEqual(self.independent()[1][0][2], "PROCESSED")
        self.assertFalse(self.post().json()["readback_confirmed"])

    def test_queue_processed_flag_alone_is_not_external_proof(self):
        self.assertEqual(self.post().status_code, 200)
        with psycopg.connect(self.dsn) as conn:
            conn.execute("UPDATE todo_bus.queue_events SET state='PROCESSED' WHERE idempotency_key=%s",
                         (queue_key(self.key),))
        self.assertEqual(self.post().status_code, 503)
        self.assertEqual(self.independent()[0]["projection"]["state"], "QUEUED")

    def test_missing_or_duplicate_target_never_creates_page(self):
        for flag in ("gone", "duplicate"):
            with self.subTest(flag=flag):
                setattr(self.remote, flag, True)
                self.assertEqual(self.post().status_code, 409)
                setattr(self.remote, flag, False)
        self.assertEqual(self.independent(), (None, []))
        self.assertNotIn(("POST", "/v1/pages"), self.remote.calls)

    def test_wrong_parent_or_archived_target_blocks(self):
        self.remote.page["parent"]["data_source_id"] = str(uuid4())
        self.assertEqual(self.post().status_code, 409)
        self.remote.page["parent"]["data_source_id"] = self.remote.data_source
        self.remote.page["archived"] = True
        self.assertEqual(self.post().status_code, 503)
        self.assertEqual(self.independent(), (None, []))

    def test_pending_projection_blocks_next_request(self):
        self.assertEqual(self.post().status_code, 200)
        next_request = {**self.body, "request_id": "next-" + uuid4().hex, "expected_version": 1}
        self.assertEqual(self.post(next_request).status_code, 409)
        self.assertEqual(len(self.independent()[1]), 1)

    def test_gateway_auth_and_legacy_write_route_fail_closed(self):
        self.assertEqual(self.post(token="wrong").status_code, 401)
        self.assertEqual(self.post(action="complete").status_code, 404)
        self.assertEqual(self.post({**self.body, "limit": 11}).status_code, 422)
        self.assertEqual(self.post({**self.body, "inventory": ["other/repository"]}).status_code, 422)
        self.assertEqual(self.independent(), (None, []))
        self.assertEqual(self.remote.calls, [])

    def test_rate_limit_uses_existing_queue_delay(self):
        self.assertEqual(self.post().status_code, 200)
        self.remote.patch_status = 429
        self.assertEqual(process_once(self.queue, self.sink)["retried"], 1)
        with psycopg.connect(self.dsn) as conn:
            seconds = conn.execute("SELECT extract(epoch FROM available_at-clock_timestamp()) FROM todo_bus.queue_events WHERE idempotency_key=%s",
                                   (queue_key(self.key),)).fetchone()[0]
        self.assertGreater(seconds, 100)
        self.assertEqual(self.remote.patches, 0)

    def test_schema_contains_typed_projection_without_weakening_original_key(self):
        schema = json.loads((Path(__file__).resolve().parents[1] / "schemas/todo-event-v1.schema.json").read_text())
        self.assertEqual(schema["properties"]["idempotency_key"]["pattern"], "^[a-f0-9]{64}$")
        definition = schema["properties"]["todo"]["properties"]["triage_projection"]
        self.assertFalse(definition["additionalProperties"])
        self.assertIn("canonical_key", definition["required"])
        self.assertIn("request_id", definition["required"])
        self.assertEqual(self.post().status_code, 200)
        event = TodoEvent.from_dict(self.independent()[1][0][1])
        self.assertEqual(projection_meta(event)["canonical_key"], self.key)

    def test_runtime_missing_configuration_blocks_without_source_requests(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {"TODO_TRIAGE_ENVIRONMENT": "DEV"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "configuration is incomplete"):
                publisher_from_env(self.queue)
        self.assertEqual(self.remote.calls, [])


    def test_uncommitted_queue_envelope_never_reaches_notion(self):
        self.assertEqual(self.post().status_code, 200)
        with psycopg.connect(self.dsn) as conn:
            conn.execute("DELETE FROM todo_bus.triage_reservations WHERE scope_key=%s",
                         (scope_for(self.key),))
        calls = len(self.remote.calls)
        self.assertEqual(process_once(self.queue, self.sink)["dlq"], 1)
        self.assertEqual(len(self.remote.calls), calls)
        self.assertEqual(self.remote.patches, 0)

    def test_concurrent_identical_requests_have_one_durable_event(self):
        barrier = threading.Barrier(2)
        def invoke(_):
            barrier.wait(timeout=3)
            return self.post()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(invoke, range(2)))
        self.assertIn(200, [r.status_code for r in results])
        self.assertTrue(all(r.status_code in {200, 409} for r in results))
        state, events = self.independent()
        self.assertEqual(len(events), 1)
        self.assertEqual(state["version"], 1)
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(process_once(self.queue, self.sink)["processed"], 1)
        self.assertTrue(self.post().json()["readback_confirmed"])
        self.assertEqual(self.remote.patches, 1)


if __name__ == "__main__":
    unittest.main()

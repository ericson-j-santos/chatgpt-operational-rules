"""Real HTTP/PostgreSQL E2E with synthetic portfolio data; never live Notion.

Requires a disposable CI database. Missing/unsafe database configuration fails;
there are deliberately no skipped tests or in-memory database replacements.
"""
from __future__ import annotations

import copy
import importlib.util
import os
import socket
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
import uvicorn

from services.todo_gateway.repository import PostgresQueueRepository
from services.todo_gateway.triage_reservation import initialize_schema, scope_for

SOURCE = "2026-10-09T19:56:00.244Z"
TEST_TOKEN = "disposable-triage-test-not-a-production-secret"


def load_asgi(name):
    path = Path(__file__).resolve().parents[1] / "services/todo_gateway/asgi.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.app


class SharedReservationE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dsn = os.environ.get("TRIAGE_TEST_DSN", "")
        config = conninfo_to_dict(cls.dsn) if cls.dsn else {}
        if (os.environ.get("GITHUB_ACTIONS") != "true" or config.get("host") != "127.0.0.1"
                or config.get("dbname") != "todo_reservation_test"):
            raise RuntimeError("E2E requires the dedicated disposable CI PostgreSQL database")
        queue = PostgresQueueRepository(cls.dsn)
        initialize_schema(queue)
        initialize_schema(queue)
        with patch.dict(os.environ, {"DATABASE_URL": cls.dsn, "TODO_GATEWAY_TOKEN": TEST_TOKEN,
                                    "TODO_TRIAGE_MODE": "dev", "TODO_TRIAGE_ENVIRONMENT": "DEV"}):
            app = load_asgi("triage_e2e_asgi")
        cls.sock = socket.socket()
        cls.sock.bind(("127.0.0.1", 0))
        cls.sock.listen(128)
        cls.url = f"http://127.0.0.1:{cls.sock.getsockname()[1]}"
        cls.server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, lifespan="off"))
        cls.thread = threading.Thread(target=cls.server.run, kwargs={"sockets": [cls.sock]}, daemon=True)
        cls.thread.start()
        deadline = time.monotonic() + 5
        while not cls.server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        if not cls.server.started:
            cls.server.should_exit = True
            cls.thread.join(5)
            cls.sock.close()
            raise RuntimeError("real HTTP server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(5)
        cls.sock.close()
        if cls.thread.is_alive():
            raise RuntimeError("HTTP server cleanup failed")

    def setUp(self):
        self.key = "triage-e2e-" + uuid.uuid4().hex
        self.inventory = [f"fixture/repository-{n:02d}" for n in range(10)]
        self.base = {"todo_key": self.key, "claim_id": "agent-a-" + uuid.uuid4().hex,
                     "correlation_id": "corr-" + uuid.uuid4().hex, "expected_version": 0,
                     "source_version": SOURCE, "inventory": self.inventory, "known": []}

    def post(self, action, body, token=TEST_TOKEN):
        return httpx.post(self.url + "/v1/triage/" + action, json=body,
                          headers={"Authorization": "Bearer " + token}, timeout=8)

    def claim(self, **changes):
        body = {**copy.deepcopy(self.base), **changes}
        response = self.post("claim", body)
        self.assertEqual(response.status_code, 200, response.text)
        return body, response.json()

    def complete_body(self, body, receipt):
        return {"todo_key": self.key, "claim_id": body["claim_id"], "fence": receipt["fence"],
                "expected_version": receipt["version"], "source_version": SOURCE,
                "commit_id": "commit-" + uuid.uuid4().hex,
                "records": [{"repository": name, "head_sha": "a" * 40, "branch": "main"}
                            for name in receipt["candidates"]]}

    def raw_state(self):
        # Independent SQL connection, not the repository or HTTP response under test.
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute("SELECT checkpoint FROM todo_bus.triage_reservations WHERE scope_key=%s",
                               (scope_for(self.key),)).fetchone()
        return row[0] if row else None

    def simultaneous(self, action, bodies):
        barrier = threading.Barrier(2)
        def invoke(body):
            barrier.wait(timeout=3)
            return self.post(action, body)
        with ThreadPoolExecutor(max_workers=2) as executor:
            return list(executor.map(invoke, bodies))

    def test_two_agents_ten_repositories_are_persisted_once(self):
        competitor = {**self.base, "claim_id": "agent-b-" + uuid.uuid4().hex}
        responses = self.simultaneous("claim", [self.base, competitor])
        self.assertEqual(sorted(r.status_code for r in responses), [200, 409])
        winner = 0 if responses[0].status_code == 200 else 1
        body = [self.base, competitor][winner]
        completed = self.complete_body(body, responses[winner].json())
        self.assertEqual(self.post("complete", completed).status_code, 200)
        replay = self.post("complete", completed)
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["replayed"])
        state = self.raw_state()
        self.assertEqual(set(state["records"]), set(self.inventory))
        self.assertEqual(len(state["records"]), 10)
        self.assertEqual(state["version"], 1)
        self.assertIsNone(state["active"])
        next_claim = {**competitor, "claim_id": "agent-next-" + uuid.uuid4().hex, "expected_version": 1}
        self.assertEqual(self.post("claim", next_claim).json()["state"], "NO_WORK")
        self.assertEqual(state, self.raw_state())
        print("TRIAGE_RACE_OK agents=2 unique_repositories=10 duplicates=0 version=1 independent_sql=true live_notion=false")

    def test_concurrent_commit_replay_does_not_increment_twice(self):
        body, receipt = self.claim()
        completed = self.complete_body(body, receipt)
        responses = self.simultaneous("complete", [completed, completed])
        self.assertEqual([r.status_code for r in responses], [200, 200])
        self.assertEqual(sorted(r.json()["replayed"] for r in responses), [False, True])
        state = self.raw_state()
        self.assertEqual(state["version"], 1)
        self.assertEqual(len(state["records"]), 10)
        self.assertEqual(len(state["receipts"]), 1)

    def test_claim_replay_does_not_extend_lease(self):
        body, first = self.claim()
        before = self.raw_state()
        second = self.post("claim", body)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["lease_until"], first["lease_until"])
        self.assertTrue(second.json()["replayed"])
        self.assertEqual(before, self.raw_state())

    def test_expiration_recovery_fences_old_worker(self):
        body, old = self.claim()
        with psycopg.connect(self.dsn) as conn:
            conn.execute("""UPDATE todo_bus.triage_reservations
                SET checkpoint=jsonb_set(checkpoint, ARRAY['claims',%s,'lease_until'], '0'::jsonb)
                WHERE scope_key=%s""", (body["claim_id"], scope_for(self.key)))
        self.assertEqual(self.post("claim", body).status_code, 409)
        self.assertEqual(self.post("complete", self.complete_body(body, old)).status_code, 409)
        new_body, new = self.claim(claim_id="new-agent-" + uuid.uuid4().hex)
        self.assertGreater(new["fence"], old["fence"])
        release_old = {"todo_key": self.key, "claim_id": body["claim_id"], "fence": old["fence"], "expected_version": 0}
        self.assertEqual(self.post("release", release_old).status_code, 409)
        self.assertEqual(self.post("complete", self.complete_body(new_body, new)).status_code, 200)
        self.assertEqual(len(self.raw_state()["records"]), 10)

    def test_release_is_idempotent_and_cannot_release_another_claim(self):
        body, receipt = self.claim()
        release = {"todo_key": self.key, "claim_id": body["claim_id"], "fence": receipt["fence"], "expected_version": 0}
        self.assertEqual(self.post("release", release).status_code, 200)
        before = self.raw_state()
        self.assertTrue(self.post("release", release).json()["replayed"])
        self.assertEqual(before, self.raw_state())
        self.claim(claim_id="another-agent-" + uuid.uuid4().hex)
        self.assertEqual(self.post("release", release).status_code, 409)

    def test_stale_checkpoint_or_source_version_never_writes(self):
        body, receipt = self.claim()
        valid = self.complete_body(body, receipt)
        for changes in ({"expected_version": 1}, {"source_version": "2026-10-09T20:00:00Z"}, {"fence": receipt["fence"] + 1}):
            before = self.raw_state()
            self.assertEqual(self.post("complete", {**valid, **changes}).status_code, 409)
            self.assertEqual(before, self.raw_state())
        self.assertEqual(self.post("complete", valid).status_code, 200)

    def test_commit_id_cannot_replace_evidence(self):
        body, receipt = self.claim()
        completed = self.complete_body(body, receipt)
        self.assertEqual(self.post("complete", completed).status_code, 200)
        before = self.raw_state()
        completed["records"][0]["head_sha"] = "b" * 40
        self.assertEqual(self.post("complete", completed).status_code, 409)
        self.assertEqual(before, self.raw_state())

    def test_out_of_scope_and_duplicate_records_are_rejected(self):
        body, receipt = self.claim(limit=2)
        completed = self.complete_body(body, receipt)
        before = self.raw_state()
        outside = copy.deepcopy(completed)
        outside["records"][0]["repository"] = self.inventory[9]
        self.assertEqual(self.post("complete", outside).status_code, 409)
        duplicate = copy.deepcopy(completed)
        duplicate["records"].append(duplicate["records"][0])
        self.assertEqual(self.post("complete", duplicate).status_code, 422)
        self.assertEqual(before, self.raw_state())

    def test_known_baseline_is_not_recounted_as_new_work(self):
        body, receipt = self.claim(known=self.inventory[:2])
        self.assertEqual(len(receipt["candidates"]), 8)
        response = self.post("complete", self.complete_body(body, receipt))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["unique_count"], 10)
        state = self.raw_state()
        self.assertEqual(len(state["records"]), 8)
        self.assertEqual(state["known"], self.inventory[:2])

    def test_initial_snapshot_must_be_consistent_and_aliases_unique(self):
        cases = [{"known": ["other/repository"]},
                 {"inventory": ["Fixture/Repo", "fixture/repo"]},
                 {"inventory": ["../repo"]}, {"expected_version": True},
                 {"lease_seconds": 301}, {"limit": 0}, {"source_version": "undated"},
                 {"extra": "not-allowed"}]
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertEqual(self.post("claim", {**self.base, **changes}).status_code, 422)
                self.assertIsNone(self.raw_state())

    def test_claim_identity_and_snapshot_drift_are_conflicts(self):
        body, receipt = self.claim()
        before = self.raw_state()
        self.assertEqual(self.post("claim", {**body, "limit": 1}).status_code, 409)
        self.assertEqual(self.post("claim", {**body, "inventory": self.inventory[:-1]}).status_code, 409)
        self.assertEqual(before, self.raw_state())

    def test_database_write_failure_rolls_back_then_recovers(self):
        body, receipt = self.claim()
        completed = self.complete_body(body, receipt)
        before = self.raw_state()
        # Fault injection affects only this scoped row in the disposable database.
        with psycopg.connect(self.dsn) as conn:
            statement = sql.SQL("""ALTER TABLE todo_bus.triage_reservations ADD CONSTRAINT e2e_reject_checkpoint
                CHECK (todo_key <> {} OR (checkpoint->>'version')::integer = 0)""").format(sql.Literal(self.key))
            conn.execute(statement)
        try:
            self.assertEqual(self.post("complete", completed).status_code, 503)
            self.assertEqual(before, self.raw_state())
        finally:
            with psycopg.connect(self.dsn) as conn:
                conn.execute("ALTER TABLE todo_bus.triage_reservations DROP CONSTRAINT e2e_reject_checkpoint")
        self.assertEqual(self.post("complete", completed).status_code, 200)
        self.assertEqual(self.raw_state()["version"], 1)

    def test_authentication_body_limit_and_unknown_action_fail_closed(self):
        self.assertEqual(self.post("claim", self.base, token="wrong").status_code, 401)
        self.assertEqual(self.post("unregistered", self.base).status_code, 404)
        self.assertEqual(self.post("claim", {**self.base, "padding": "x" * 65536}).status_code, 413)
        self.assertEqual(self.post("claim", []).status_code, 422)
        response = httpx.get(self.url + "/v1/triage/snapshot", params={"todo_key": self.key}, timeout=5)
        self.assertEqual(response.status_code, 401)
        self.assertIsNone(self.raw_state())

    def test_independent_http_snapshot_matches_sql(self):
        body, receipt = self.claim()
        self.assertEqual(self.post("complete", self.complete_body(body, receipt)).status_code, 200)
        response = httpx.get(self.url + "/v1/triage/snapshot", params={"todo_key": self.key},
                             headers={"Authorization": "Bearer " + TEST_TOKEN}, timeout=5)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reviewed"], sorted(self.raw_state()["records"]))
        self.assertEqual(response.json()["unique_count"], 10)
        self.assertFalse(response.json()["todo_completed"])
        self.assertEqual(response.json()["external_projection"], "PENDING")

    def test_existing_deployment_default_and_environment_gate(self):
        with patch.dict(os.environ, {"DATABASE_URL": self.dsn, "TODO_GATEWAY_TOKEN": TEST_TOKEN,
                                    "TODO_TRIAGE_MODE": "disabled"}):
            app = load_asgi("triage_disabled_asgi")
            self.assertFalse(any(route.path.startswith("/v1/triage") for route in app.routes))
        with patch.dict(os.environ, {"DATABASE_URL": self.dsn, "TODO_GATEWAY_TOKEN": TEST_TOKEN,
                                    "TODO_TRIAGE_MODE": "dev", "TODO_TRIAGE_ENVIRONMENT": "PROD"}):
            with self.assertRaises(RuntimeError):
                load_asgi("triage_wrong_environment_asgi")


if __name__ == "__main__":
    unittest.main()

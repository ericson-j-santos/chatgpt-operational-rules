from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from services.todo_gateway import service_main


VALID_ENV = {
    "DATABASE_URL": "postgresql://example.invalid/todo",
    "TODO_GATEWAY_TOKEN": "gateway-secret",
    "NOTION_TOKEN": "notion-secret",
    "NOTION_DATA_SOURCE_ID": "data-source-id",
    "PORT": "8123",
    "TODO_WORKER_POLL_SECONDS": "0.25",
}


class FakeProcess:
    def __init__(self, poll_values):
        self._poll_values = list(poll_values)
        self.terminated = False
        self.killed = False
        self.waited = False

    def poll(self):
        if len(self._poll_values) > 1:
            return self._poll_values.pop(0)
        return self._poll_values[0]

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.waited = True
        return self.poll()


class ServiceMainTests(unittest.TestCase):
    def test_missing_required_environment_fails_before_bootstrap(self):
        env = dict(VALID_ENV)
        env.pop("NOTION_TOKEN")

        with patch.object(service_main, "bootstrap_schema") as bootstrap:
            with self.assertRaisesRegex(RuntimeError, "NOTION_TOKEN"):
                service_main.run_supervised(env, popen=MagicMock())
            bootstrap.assert_not_called()

    def test_bootstrap_schema_executes_migration(self):
        fake_conn = MagicMock()
        fake_cm = MagicMock()
        fake_cm.__enter__.return_value = fake_conn
        fake_cm.__exit__.return_value = False
        fake_psycopg = SimpleNamespace(connect=MagicMock(return_value=fake_cm))

        with tempfile.TemporaryDirectory() as tmp:
            schema = Path(tmp) / "schema.sql"
            schema.write_text("SELECT 1;", encoding="utf-8")
            with patch.dict(sys.modules, {"psycopg": fake_psycopg}):
                service_main.bootstrap_schema("postgresql://dsn", schema)

        fake_psycopg.connect.assert_called_once_with("postgresql://dsn")
        fake_conn.execute.assert_called_once_with("SELECT 1;")

    def test_worker_failure_stops_web_and_returns_worker_code(self):
        worker = FakeProcess([3])
        web = FakeProcess([None, None, 0])
        created = []

        def popen(cmd, env):
            created.append((cmd, env))
            return worker if len(created) == 1 else web

        with patch.object(service_main, "bootstrap_schema") as bootstrap:
            rc = service_main.run_supervised(
                VALID_ENV,
                popen=popen,
                sleep=lambda _: None,
            )

        self.assertEqual(rc, 3)
        bootstrap.assert_called_once_with(VALID_ENV["DATABASE_URL"])
        self.assertTrue(web.terminated)
        self.assertIn("services.todo_gateway.worker", created[0][0])
        self.assertIn("services.todo_gateway.asgi:app", created[1][0])
        self.assertEqual(created[1][0][-1], "8123")

    def test_clean_child_exit_is_treated_as_failure_and_stops_peer(self):
        worker = FakeProcess([None, None, 0])
        web = FakeProcess([0])
        created = []

        def popen(cmd, env):
            created.append(cmd)
            return worker if len(created) == 1 else web

        with patch.object(service_main, "bootstrap_schema"):
            rc = service_main.run_supervised(
                VALID_ENV,
                popen=popen,
                sleep=lambda _: None,
            )

        self.assertEqual(rc, 1)
        self.assertTrue(worker.terminated)


if __name__ == "__main__":
    unittest.main()

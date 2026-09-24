from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEDULER_COMPOSE = ROOT / "docker-compose.scheduler.pc24x7.yml"
from unittest.mock import patch

from scripts.github_schedule_bridge import (
    BridgeError,
    WorkflowRun,
    _scheduler_event,
    latest_successful_run,
    process_tick,
    run_once,
)


def run(run_id: int = 101) -> WorkflowRun:
    return WorkflowRun(
        run_id=run_id,
        event="schedule",
        head_branch="main",
        head_sha="a" * 40,
        html_url=f"https://github.com/example/actions/runs/{run_id}",
        created_at="2026-09-18T17:00:00Z",
    )


class GitHubScheduleBridgeTests(unittest.TestCase):

    def test_scheduler_compose_is_isolated_from_runtime_topology(self):
        text = SCHEDULER_COMPOSE.read_text(encoding="utf-8")
        self.assertIn("name: todo-global-scheduler", text)
        self.assertIn("restart: unless-stopped", text)
        self.assertIn("TODO_GATEWAY_URL: http://gateway:8000", text)
        self.assertIn("GITHUB_SCHEDULE_WORKFLOW: todo-global-hourly-cycle.yml", text)
        self.assertIn("todo_global_scheduler_state", text)
        self.assertIn("external: true", text)
        self.assertIn("name: todo-global-24x7_default", text)
        self.assertNotIn("postgres:16-alpine", text)
        self.assertNotIn("GITHUB_TOKEN:", text)

    @patch("scripts.github_schedule_bridge._json_request")
    def test_latest_successful_run_filters_branch_event_and_conclusion(self, request):
        request.return_value = {
            "workflow_runs": [
                {"id": 104, "event": "push", "head_branch": "main", "head_sha": "d" * 40, "html_url": "x", "created_at": "x", "conclusion": "success"},
                {"id": 103, "event": "schedule", "head_branch": "feature", "head_sha": "c" * 40, "html_url": "x", "created_at": "x", "conclusion": "success"},
                {"id": 102, "event": "workflow_dispatch", "head_branch": "main", "head_sha": "b" * 40, "html_url": "y", "created_at": "y", "conclusion": "success"},
                {"id": 101, "event": "schedule", "head_branch": "main", "head_sha": "a" * 40, "html_url": "z", "created_at": "z", "conclusion": "success"},
            ]
        }
        result = latest_successful_run("owner/repo", "workflow.yml", "main")
        self.assertIsNotNone(result)
        self.assertEqual(result.run_id, 102)
        self.assertEqual(result.event, "workflow_dispatch")

    def test_scheduler_event_is_deterministic_for_same_run(self):
        first, first_key = _scheduler_event(run(777))
        second, second_key = _scheduler_event(run(777))
        self.assertEqual(first["event_id"], "evt-gh-hourly-777")
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(first["correlation_id"], second["correlation_id"])
        self.assertEqual(first_key, second_key)

    @patch("scripts.github_schedule_bridge._gateway_json")
    def test_process_tick_dispatches_one_highest_priority_oldest_todo(self, gateway):
        scheduler_event, scheduler_key = _scheduler_event(run())
        p2_old_key = "1" * 64
        p0_new_key = "2" * 64
        p0_old_key = "3" * 64
        existing_key = "4" * 64
        terminal_key = "5" * 64
        untyped_key = "6" * 64
        not_ready_key = "7" * 64

        def side_effect(method, gateway_url, token, path, payload=None):
            if method == "POST" and path == "/v1/events":
                self.assertEqual(payload["event_id"], scheduler_event["event_id"])
                return {"accepted": True, "duplicate": False}
            if method == "GET" and path == "/v1/todos?limit=200":
                return {
                    "items": [
                        {"event_id": "evt-scheduler", "idempotency_key": scheduler_key, "todo": {"status": "EM ANDAMENTO"}},
                        {
                            "event_id": "evt-p2-old",
                            "idempotency_key": p2_old_key,
                            "created_at": "2026-09-18T08:00:00Z",
                            "todo": {"status": "PENDENTE", "priority": "P2", "automation_action": "safe.action.v1"},
                        },
                        {
                            "event_id": "evt-p0-new",
                            "idempotency_key": p0_new_key,
                            "created_at": "2026-09-18T11:00:00Z",
                            "todo": {"status": "PENDENTE", "priority": "P0", "automation_action": "safe.action.v1"},
                        },
                        {
                            "event_id": "evt-p0-old",
                            "idempotency_key": p0_old_key,
                            "created_at": "2026-09-18T10:00:00Z",
                            "todo": {
                                "status": "PENDENTE",
                                "priority": "P0",
                                "automation_state": "READY_FOR_AI",
                                "automation_action": "safe.action.v1",
                            },
                        },
                        {
                            "event_id": "evt-existing",
                            "idempotency_key": existing_key,
                            "todo": {"status": "EM ANDAMENTO", "priority": "P0", "automation_action": "safe.action.v1"},
                        },
                        {
                            "event_id": "evt-terminal",
                            "idempotency_key": terminal_key,
                            "todo": {"status": "CONCLUÍDO", "priority": "P0", "automation_action": "safe.action.v1"},
                        },
                        {"event_id": "evt-untyped", "idempotency_key": untyped_key, "todo": {"status": "PENDENTE", "priority": "P0"}},
                        {
                            "event_id": "evt-not-ready",
                            "idempotency_key": not_ready_key,
                            "todo": {
                                "status": "PENDENTE",
                                "priority": "P0",
                                "automation_state": "HOLD",
                                "automation_action": "safe.action.v1",
                            },
                        },
                    ]
                }
            if method == "GET" and path == "/v1/continuations?limit=200":
                return {"items": [{"basis_event_id": "evt-existing"}]}
            if method == "POST" and path == f"/v1/todos/{p0_old_key}/continue":
                self.assertEqual(payload["correlation_id"], f"gh-hourly-101-{p0_old_key[:12]}")
                return {"duplicate": False, "request_id": "cont-p0-old"}
            raise AssertionError((method, path))

        gateway.side_effect = side_effect
        result = process_tick(run(), "http://gateway:8000", "token")

        self.assertEqual(result["requested"], 1)
        self.assertEqual(result["duplicate_or_existing"], 1)
        self.assertEqual(result["skipped_terminal"], 1)
        self.assertEqual(result["skipped_untyped"], 1)
        self.assertEqual(result["skipped_not_ready"], 1)
        self.assertEqual(result["eligible"], 3)
        self.assertEqual(result["skipped_by_capacity"], 2)
        self.assertEqual(result["selected_idempotency_key"], p0_old_key)

    @patch("scripts.github_schedule_bridge.process_tick")
    @patch("scripts.github_schedule_bridge.latest_successful_run")
    def test_state_prevents_reprocessing_same_github_run(self, latest, process):
        latest.return_value = run(333)
        process.return_value = {
            "requested": 1,
            "duplicate_or_existing": 0,
            "skipped_terminal": 0,
            "skipped_untyped": 0,
            "run_id": 333,
        }
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            first = run_once(
                repository="owner/repo",
                workflow="workflow.yml",
                branch="main",
                gateway_url="http://gateway",
                gateway_token="token",
                state_file=state,
            )
            second = run_once(
                repository="owner/repo",
                workflow="workflow.yml",
                branch="main",
                gateway_url="http://gateway",
                gateway_token="token",
                state_file=state,
            )

        self.assertEqual(first["result"], "PROCESSED")
        self.assertEqual(second, {"result": "ALREADY_PROCESSED", "run_id": 333})
        self.assertEqual(process.call_count, 1)

    @patch("scripts.github_schedule_bridge.process_tick", side_effect=BridgeError("gateway down"))
    @patch("scripts.github_schedule_bridge.latest_successful_run")
    def test_state_is_not_advanced_when_processing_fails(self, latest, process):
        latest.return_value = run(444)
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            with self.assertRaisesRegex(BridgeError, "gateway down"):
                run_once(
                    repository="owner/repo",
                    workflow="workflow.yml",
                    branch="main",
                    gateway_url="http://gateway",
                    gateway_token="token",
                    state_file=state,
                )
            self.assertFalse(state.exists())


if __name__ == "__main__":
    unittest.main()

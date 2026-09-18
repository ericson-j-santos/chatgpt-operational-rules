from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
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
    def test_process_tick_requests_only_new_typed_nonterminal_todos(self, gateway):
        scheduler_event, scheduler_key = _scheduler_event(run())
        typed_key = "1" * 64
        existing_key = "2" * 64
        terminal_key = "3" * 64
        untyped_key = "4" * 64

        def side_effect(method, gateway_url, token, path, payload=None):
            if method == "POST" and path == "/v1/events":
                self.assertEqual(payload["event_id"], scheduler_event["event_id"])
                return {"accepted": True, "duplicate": False}
            if method == "GET" and path == "/v1/todos?limit=200":
                return {
                    "items": [
                        {"event_id": "evt-scheduler", "idempotency_key": scheduler_key, "todo": {"status": "EM ANDAMENTO"}},
                        {"event_id": "evt-typed", "idempotency_key": typed_key, "todo": {"status": "PENDENTE", "automation_action": "safe.action.v1"}},
                        {"event_id": "evt-existing", "idempotency_key": existing_key, "todo": {"status": "EM ANDAMENTO", "automation_action": "safe.action.v1"}},
                        {"event_id": "evt-terminal", "idempotency_key": terminal_key, "todo": {"status": "CONCLUÍDO", "automation_action": "safe.action.v1"}},
                        {"event_id": "evt-untyped", "idempotency_key": untyped_key, "todo": {"status": "PENDENTE"}},
                    ]
                }
            if method == "GET" and path == "/v1/continuations?limit=200":
                return {"items": [{"basis_event_id": "evt-existing"}]}
            if method == "POST" and path == f"/v1/todos/{typed_key}/continue":
                self.assertEqual(payload["correlation_id"], f"gh-hourly-101-{typed_key[:12]}")
                return {"duplicate": False, "request_id": "cont-typed"}
            raise AssertionError((method, path))

        gateway.side_effect = side_effect
        result = process_tick(run(), "http://gateway:8000", "token")

        self.assertEqual(result["requested"], 1)
        self.assertEqual(result["duplicate_or_existing"], 1)
        self.assertEqual(result["skipped_terminal"], 1)
        self.assertEqual(result["skipped_untyped"], 1)

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

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from scripts.continuation_worker import Continuation, HumanGate, PostgresContinuationQueue, execute, process_batch, process_cycle


class FakeQueue:
    def __init__(self, items):
        self.items = items
        self.transitions = []
        self.reserve_calls = 0

    def reserve_continuations(self, limit, lease_seconds):
        self.reserve_calls += 1
        return self.items[:limit]

    def complete_continuation(self, request_id):
        self.transitions.append((request_id, "COMPLETED")); return True

    def gate_continuation(self, request_id, detail):
        self.transitions.append((request_id, "HUMAN_GATE", detail)); return True

    def fail_continuation(self, request_id, error, max_attempts, backoff_seconds):
        state = "DLQ" if next(x for x in self.items if x.request_id == request_id).attempts >= max_attempts else "PENDING"
        self.transitions.append((request_id, state, error)); return state


def item(request_id, attempts=1):
    return Continuation(request_id, "a" * 64, "corr-test-001", "test", {"automation_action": "safe.test"}, attempts)


class WorkerTest(unittest.TestCase):
    def test_postgres_heartbeat_uses_canonical_database_function(self):
        queue = PostgresContinuationQueue("postgresql://example.invalid/test")
        connection = MagicMock()
        cursor = MagicMock()
        connection.__enter__.return_value = connection
        connection.cursor.return_value.__enter__.return_value = cursor

        with patch.object(queue, "_connect", return_value=connection):
            queue.heartbeat("desktop-worker-01", {"completed": 1, "retry": 0})

        cursor.execute.assert_called_once_with(
            "SELECT todo_bus.touch_worker_heartbeat(%s,%s::jsonb)",
            ("desktop-worker-01", '{"completed": 1, "retry": 0}'),
        )

    @patch("scripts.continuation_worker.execute")
    def test_success_does_not_block_next_item(self, execute):
        q = FakeQueue([item("one"), item("two")])
        result = process_batch(q)
        self.assertEqual(result, {"reserved": 2, "completed": 2, "human_gate": 0, "retry": 0, "dlq": 0})
        self.assertEqual(len(q.transitions), 2)

    @patch("scripts.continuation_worker.execute", side_effect=HumanGate("approval_required"))
    def test_human_gate_isolated_per_item(self, execute):
        q = FakeQueue([item("one"), item("two")])
        result = process_batch(q)
        self.assertEqual(result["human_gate"], 2)
        self.assertEqual([x[1] for x in q.transitions], ["HUMAN_GATE", "HUMAN_GATE"])

    @patch("scripts.continuation_worker.execute", side_effect=RuntimeError("known failure"))
    def test_retry_then_dlq_by_attempt_count(self, execute):
        q = FakeQueue([item("retry", 1), item("dead", 3)])
        result = process_batch(q, max_attempts=3)
        self.assertEqual(result["retry"], 1)
        self.assertEqual(result["dlq"], 1)
        self.assertEqual([x[1] for x in q.transitions], ["PENDING", "DLQ"])

    @patch("scripts.continuation_worker.execute")
    def test_estudo_profile_does_not_reserve_new_work(self, execute):
        q = FakeQueue([item("one")])
        result = process_cycle(q, profile="ESTUDO")
        self.assertEqual(result["study_mode"], 1)
        self.assertEqual(result["reserved"], 0)
        self.assertEqual(q.reserve_calls, 0)
        execute.assert_not_called()

    @patch("scripts.continuation_worker.execute")
    def test_normal_profile_processes_work(self, execute):
        q = FakeQueue([item("one")])
        result = process_cycle(q, profile="NORMAL")
        self.assertEqual(result["study_mode"], 0)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(q.reserve_calls, 1)

    @patch("scripts.continuation_worker.execute")
    def test_invalid_profile_fails_closed_without_reservation(self, execute):
        q = FakeQueue([item("one")])
        result = process_cycle(q, profile="INVALID")
        self.assertEqual(result["profile_blocked"], 1)
        self.assertEqual(q.reserve_calls, 0)
        execute.assert_not_called()

    def test_registered_action_accepts_only_known_e2e_payload(self):
        x = Continuation("ok", "a"*64, "corr", "AI Control Plane", {"automation_action":"ai_control_plane.validate_idempotent_continuation.v1","next_action":"validar continuidade idempotente","external_id":"desktop-24x7-e2e-abc"}, 1)
        self.assertIsNone(execute(x))

    def test_registered_action_rejects_wrong_project(self):
        x = Continuation("bad", "a"*64, "corr", "AI Hub", {"automation_action":"ai_control_plane.validate_idempotent_continuation.v1","next_action":"validar continuidade idempotente","external_id":"desktop-24x7-e2e-abc"}, 1)
        with self.assertRaisesRegex(HumanGate, "project_not_allowed"):
            execute(x)

    def test_unknown_action_remains_human_gate(self):
        x = Continuation("bad", "a"*64, "corr", "AI Control Plane", {"automation_action":"unknown.action"}, 1)
        with self.assertRaisesRegex(HumanGate, "not_registered"):
            execute(x)


    def test_execution_lane_action_dispatches_typed_request(self):
        request = {
            "repository": "ericson-j-santos/example",
            "issue_number": 90,
            "request_id": "todo-execution-90",
            "base_sha": "b" * 40,
            "priority": 10,
        }
        x = Continuation(
            "cont-90",
            "a" * 64,
            "corr-execution-90",
            "Example",
            {
                "automation_action": "execution_lane.enqueue.v1",
                "external_id": "todo-execution-90",
                "execution_request": request,
            },
            1,
        )
        response = {"created": True, "task": {**request, "task_id": "task-90"}}
        with patch.dict(
            os.environ,
            {
                "EXECUTION_LANE_BASE_URL": "http://127.0.0.1:8097",
                "EXECUTION_LANE_API_TOKEN_FILE": "/tmp/token",
            },
            clear=False,
        ), patch("scripts.continuation_worker.ExecutionLaneClient") as client_type:
            client_type.return_value.enqueue.return_value = response
            self.assertIsNone(execute(x))
            client_type.return_value.enqueue.assert_called_once_with(request)

    def test_execution_lane_action_rejects_request_id_mismatch(self):
        x = Continuation(
            "cont-bad",
            "a" * 64,
            "corr-execution-bad",
            "Example",
            {
                "automation_action": "execution_lane.enqueue.v1",
                "external_id": "stable-id",
                "execution_request": {
                    "repository": "ericson-j-santos/example",
                    "issue_number": 90,
                    "request_id": "different-id",
                    "base_sha": "b" * 40,
                },
            },
            1,
        )
        with self.assertRaisesRegex(HumanGate, "request_id_mismatch"):
            execute(x)

    def test_execution_lane_action_rejects_invalid_payload_before_io(self):
        x = Continuation(
            "cont-invalid",
            "a" * 64,
            "corr-execution-invalid",
            "Example",
            {
                "automation_action": "execution_lane.enqueue.v1",
                "execution_request": {"repository": "invalid"},
            },
            1,
        )
        with patch("scripts.continuation_worker.ExecutionLaneClient") as client_type:
            with self.assertRaisesRegex(HumanGate, "execution_request_invalid"):
                execute(x)
            client_type.assert_not_called()


if __name__ == "__main__":
    unittest.main()

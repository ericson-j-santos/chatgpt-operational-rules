from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts.continuation_worker import Continuation, HumanGate, execute, process_batch, process_cycle


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


if __name__ == "__main__":
    unittest.main()

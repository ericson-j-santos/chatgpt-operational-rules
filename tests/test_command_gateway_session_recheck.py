"""Ensure Command Gateway rechecks session membership while holding repo lock.

This reproduces the revocation race: earlier admission was successful, the
control plane retired the session while command startup waited on a lock, and
the stale session MUST NOT execute a subprocess after the lock is obtained.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import command_gateway as gateway


class RecheckTests(TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name)
        self.state = gateway.GitState(
            repo_root=str(self.repo),
            branch="DETACHED",
            head="a" * 40,
            status_digest="b" * 64,
            status_count=0,
        )
        self.policy = {
            "require_session_bootstrap": True,
            "require_git_repo": True,
            "max_timeout_seconds": 120,
            "risk2_requires_clean_tree": True,
        }
        self.session_id = "rules213-recheck-regression-20261115"

    def _execute(self, *, admitted_before_lock=True,
                 admitted_inside_lock=True, session_id=None):
        admitted = {"value": admitted_before_lock}
        calls = []

        def check(policy, sid, cwd, risk):
            calls.append((sid, risk, admitted["value"]))
            if not admitted["value"]:
                raise gateway.GatewayError("BOOTSTRAP_REQUIRED: retired session",
                                           gateway.EXIT_SESSION_REQUIRED)
            if sid != self.session_id:
                raise gateway.GatewayError("BOOTSTRAP_REQUIRED: mismatched session",
                                           gateway.EXIT_SESSION_REQUIRED)
            return {"session_id": sid, "status": "materialized"}

        @contextmanager
        def lock(*_args, **_kwargs):
            admitted["value"] = admitted_inside_lock
            yield self.repo / "lock"

        @contextmanager
        def anchored(path):
            yield path, {}

        with mock.patch.object(gateway, "enforce_session", side_effect=check), \
             mock.patch.object(gateway, "repository_lock", side_effect=lock), \
             mock.patch.object(gateway, "git_state", return_value=self.state), \
             mock.patch.object(gateway, "canonical_existing_directory",
                               return_value=self.repo), \
             mock.patch.object(gateway, "assert_allowed_path"), \
             mock.patch.object(gateway, "validate_command"), \
             mock.patch.object(gateway, "assert_safe_gui_automation"), \
             mock.patch.object(gateway, "anchored_subprocess_cwd", side_effect=anchored), \
             mock.patch.object(gateway, "subprocess") as sp, \
             mock.patch.object(gateway, "append_event"), \
             mock.patch.object(gateway, "emit_json"):
            sp.run.return_value = SimpleNamespace(
                returncode=0, stdout="", stderr="",
            )
            # The first check is done by main() in production. This test
            # makes the passage of that initial check explicit.
            if admitted_before_lock:
                check(self.policy, self.session_id, self.repo, 1)
            try:
                result = gateway.execute(
                    cwd=self.repo, policy=self.policy,
                    args=["python", "-B", "--version"], risk=1,
                    timeout=30, expected_head=self.state.head,
                    allow_dirty=False, allow_head_change=False,
                    correlation_id="rules213-recheck-locked-20261115",
                    session_id=session_id or self.session_id,
                    session_admitted_at_entry=True,
                )
                return result, calls, sp.run.call_count
            except gateway.GatewayError as exc:
                return exc, calls, sp.run.call_count

    def test_retirement_between_admission_and_lock_blocks_execution(self):
        result, calls, spawned = self._execute(
            admitted_before_lock=True, admitted_inside_lock=False,
        )
        self.assertIsInstance(result, gateway.GatewayError)
        self.assertEqual(result.exit_code, gateway.EXIT_SESSION_REQUIRED)
        self.assertEqual(spawned, 0)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0][2])
        self.assertFalse(calls[1][2])

    def test_active_session_can_execute_one_command(self):
        result, calls, spawned = self._execute()
        self.assertEqual(result, 0)
        self.assertEqual(spawned, 1)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c[2] for c in calls))

    def test_admitted_cli_execution_cannot_use_wrong_session_id(self):
        result, calls, spawned = self._execute(
            admitted_before_lock=False, admitted_inside_lock=True,
            session_id="unknown-session",
        )
        self.assertIsInstance(result, gateway.GatewayError)
        self.assertEqual(result.exit_code, gateway.EXIT_SESSION_REQUIRED)
        self.assertEqual(spawned, 0)
        self.assertEqual(len(calls), 1)

    def test_recheck_happens_before_state_read_and_process(self):
        order = []
        @contextmanager
        def lock(*_args):
            order.append("lock")
            yield Path("synthetic-lock")
        with mock.patch.object(gateway, "enforce_session",
                               side_effect=lambda *_args: order.append("recheck")), \
             mock.patch.object(gateway, "repository_lock", side_effect=lock), \
             mock.patch.object(gateway, "git_state",
                               side_effect=lambda *_args: (order.append("git") or self.state)), \
             mock.patch.object(gateway, "canonical_existing_directory",
                               return_value=self.repo), \
             mock.patch.object(gateway, "assert_allowed_path"), \
             mock.patch.object(gateway, "validate_command"), \
             mock.patch.object(gateway, "assert_safe_gui_automation"), \
             mock.patch.object(gateway, "anchored_subprocess_cwd") as anchored, \
             mock.patch.object(gateway, "subprocess") as sp, \
             mock.patch.object(gateway, "append_event"), \
             mock.patch.object(gateway, "emit_json"):
            @contextmanager
            def anchor(_):
                yield self.repo, {}
            anchored.side_effect = anchor
            sp.run.return_value = SimpleNamespace(
                returncode=0, stdout="", stderr="",
            )
            gateway.execute(
                cwd=self.repo, policy=self.policy,
                args=["python", "-B", "--version"], risk=1,
                timeout=30, expected_head=self.state.head,
                allow_dirty=False, allow_head_change=False,
                correlation_id="rules213-recheck-order-20261115",
                session_id=self.session_id,
                session_admitted_at_entry=True,
            )
        self.assertEqual(order[0], "git")
        self.assertEqual(order[1:4], ["lock", "recheck", "git"])


    def test_cli_main_always_requests_recheck_under_lock(self):
        argv = [
            "command_gateway.py", "--policy", "synthetic-policy",
            "--correlation-id", "rules213-main-binding-20261115",
            "run", "--cwd", str(self.repo),
            "--session-id", self.session_id,
            "--risk", "1", "--expected-head", "a" * 40,
            "--", "python", "-B", "--version",
        ]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(gateway, "load_policy", return_value=self.policy), \
             mock.patch.object(gateway, "canonical_existing_directory",
                               return_value=self.repo), \
             mock.patch.object(gateway, "enforce_session",
                               return_value={"session_id": self.session_id}), \
             mock.patch.object(gateway, "execute", return_value=0) as called:
            self.assertEqual(gateway.main(), 0)
        self.assertEqual(called.call_count, 1)
        self.assertTrue(called.call_args.kwargs["session_admitted_at_entry"])
        self.assertEqual(called.call_args.kwargs["session_id"], self.session_id)


if __name__ == "__main__":
    import unittest
    unittest.main()

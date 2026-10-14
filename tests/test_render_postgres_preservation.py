from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from scripts import command_gateway, continuation_worker
from scripts.render_postgres_preservation import (
    AccessCleanupFailed,
    PreservationBlocked,
    PreservationPlan,
    TrustedPreflightEvidence,
    main,
    preserve,
)


class ExecutableSpecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.tool = self.root / "pg_dump"
        self.tool.write_bytes(b"approved-binary")
        self.digest = hashlib.sha256(self.tool.read_bytes()).hexdigest()
        self.policy = {
            "allowed_executables": [],
            "blocked_executables": [],
            "allowed_executable_specs": [{"path": str(self.tool), "sha256": self.digest}],
            "denied_names": [".env"],
            "denied_segments": [],
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_exact_path_and_digest_are_required(self) -> None:
        command_gateway.validate_command([str(self.tool), "--version"], 1, self.policy)
        other = self.root / "other" / "pg_dump"
        other.parent.mkdir()
        other.write_bytes(self.tool.read_bytes())
        with self.assertRaisesRegex(command_gateway.GatewayError, "não autorizado"):
            command_gateway.validate_command([str(other), "--version"], 1, self.policy)
        bad = dict(self.policy)
        bad["allowed_executable_specs"] = [{"path": str(self.tool), "sha256": "0" * 64}]
        with self.assertRaisesRegex(command_gateway.GatewayError, "integridade"):
            command_gateway.validate_command([str(self.tool), "--version"], 1, bad)

    def test_relative_approved_spec_is_rejected_before_resolve(self) -> None:
        with self.assertRaisesRegex(command_gateway.GatewayError, "absoluto"):
            command_gateway.validate_executable_spec(
                {"path": "tools/pg_dump", "sha256": "0" * 64},
                require_exists=False,
            )

    def test_postgres_uri_is_rejected_in_real_validate_command_path(self) -> None:
        with self.assertRaisesRegex(command_gateway.GatewayError, "sensível"):
            command_gateway.validate_command(
                [str(self.tool), "--dbname=postgresql://user:credential@example/db"],
                1,
                self.policy,
            )

    def test_unauthorized_and_wrong_risk_fail_closed(self) -> None:
        unauthorized = self.root / "pg_restore"
        unauthorized.write_bytes(b"unapproved-binary")
        with self.assertRaisesRegex(command_gateway.GatewayError, "não autorizado"):
            command_gateway.validate_command([str(unauthorized)], 1, self.policy)
        with self.assertRaisesRegex(command_gateway.GatewayError, "risco 3"):
            command_gateway.validate_command([str(self.tool)], 3, self.policy)


class PreservationCoordinatorTests(unittest.TestCase):
    def plan(self, **overrides: object) -> PreservationPlan:
        values = {
            "enabled": True,
            "requested_allowlist_ip": "203.0.113.10",
            "encrypted_output": Path("/safe/backup.age"),
        }
        values.update(overrides)
        return PreservationPlan(**values)

    def evidence(self, **overrides: object) -> TrustedPreflightEvidence:
        values = {
            "approval_is_current": True,
            "credential_channel_is_ready": True,
            "observed_desktop_ip": "203.0.113.10",
        }
        values.update(overrides)
        return TrustedPreflightEvidence(**values)

    def run_preserve(self, *, plan=None, evidence=None, arguments=(), **callbacks):
        order = callbacks.pop("order", [])
        preserve(
            plan or self.plan(),
            arguments=arguments,
            load_trusted_evidence=lambda: evidence or self.evidence(),
            add_access=callbacks.pop("add_access", lambda _cidr: order.append("add")),
            export_encrypted=callbacks.pop("export_encrypted", lambda: order.append("export")),
            remove_access=callbacks.pop("remove_access", lambda _cidr: order.append("remove")),
            access_is_removed=callbacks.pop(
                "access_is_removed", lambda _cidr: order.append("readback") or True
            ),
            validate_offline_restore=callbacks.pop(
                "validate_offline_restore", lambda: order.append("restore")
            ),
        )
        return order

    def test_untrusted_or_wrong_typed_gates_block_before_network(self) -> None:
        cases = (
            (self.plan(enabled="false"), self.evidence()),
            (self.plan(), self.evidence(approval_is_current="false")),
            (self.plan(), self.evidence(credential_channel_is_ready="false")),
            (self.plan(), self.evidence(approval_is_current=1)),
            (self.plan(), self.evidence(observed_desktop_ip=2030011310)),
            (self.plan(requested_allowlist_ip=2030011310), self.evidence()),
        )
        for plan, evidence in cases:
            called: list[str] = []
            with self.assertRaises(PreservationBlocked):
                self.run_preserve(plan=plan, evidence=evidence, order=called)
            self.assertEqual([], called)

    def test_ip_mismatch_blocks_before_network(self) -> None:
        called: list[str] = []
        with self.assertRaisesRegex(PreservationBlocked, "desktop_ip_mismatch"):
            self.run_preserve(
                evidence=self.evidence(observed_desktop_ip="203.0.113.11"),
                order=called,
            )
        self.assertEqual([], called)

    def test_sensitive_uri_is_rejected_in_real_preserve_path(self) -> None:
        called: list[str] = []
        with self.assertRaisesRegex(PreservationBlocked, "sensitive_argument"):
            self.run_preserve(
                arguments=("--dbname=postgres://user:credential@example/db",),
                order=called,
            )
        self.assertEqual([], called)

    def test_access_is_removed_after_export_failure(self) -> None:
        order: list[str] = []
        def fail_export() -> None:
            order.append("export")
            raise RuntimeError("controlled export failure")
        with self.assertRaisesRegex(RuntimeError, "controlled export failure"):
            self.run_preserve(order=order, export_encrypted=fail_export)
        self.assertEqual(["add", "export", "remove", "readback"], order)

    def test_partial_add_failure_still_attempts_cleanup(self) -> None:
        order: list[str] = []
        def partial_add(_cidr: str) -> None:
            order.append("add")
            raise RuntimeError("partial add")
        with self.assertRaisesRegex(RuntimeError, "partial add"):
            self.run_preserve(order=order, add_access=partial_add)
        self.assertEqual(["add", "remove", "readback"], order)

    def test_remove_exception_is_terminal(self) -> None:
        def fail_remove(_cidr: str) -> None:
            raise RuntimeError("remove failed")
        with self.assertRaisesRegex(AccessCleanupFailed, "cleanup_failed"):
            self.run_preserve(
                remove_access=fail_remove,
                validate_offline_restore=lambda: self.fail("restore must not run"),
            )

    def test_readback_exception_is_terminal(self) -> None:
        def fail_readback(_cidr: str) -> bool:
            raise RuntimeError("readback failed")
        with self.assertRaisesRegex(AccessCleanupFailed, "cleanup_failed"):
            self.run_preserve(
                access_is_removed=fail_readback,
                validate_offline_restore=lambda: self.fail("restore must not run"),
            )

    def test_readback_requires_literal_true(self) -> None:
        for value in ("true", 1, object()):
            with self.assertRaises(AccessCleanupFailed):
                self.run_preserve(
                    access_is_removed=lambda _cidr, result=value: result,
                    validate_offline_restore=lambda: self.fail("restore must not run"),
                )

    def test_restore_runs_only_after_literal_cleanup_readback(self) -> None:
        order: list[str] = []
        self.run_preserve(order=order)
        self.assertEqual(["add", "export", "remove", "readback", "restore"], order)

    def test_main_rejects_sensitive_arguments_and_remains_dormant(self) -> None:
        with mock.patch("builtins.print") as output:
            self.assertEqual(20, main(["postgresql://user:credential@example/db"]))
            self.assertIn("sensitive_argument_rejected", output.call_args.args[0])
            self.assertEqual(20, main([]))
            self.assertIn("recovery_not_configured", output.call_args.args[0])


class DormantActionTests(unittest.TestCase):
    def item(self, todo=None) -> continuation_worker.Continuation:
        payload = {
            "automation_action": "operational_rules.render_postgres_preservation.v1",
            "repo_path": ".",
            "expected_head": "0" * 40,
        }
        payload.update(todo or {})
        return continuation_worker.Continuation(
            request_id="req", idempotency_key="idem", correlation_id="corr",
            project="AI Control Plane", todo=payload, attempts=0,
        )

    def test_execute_rejects_sensitive_payload_before_handler(self) -> None:
        called: list[str] = []
        action = "operational_rules.render_postgres_preservation.v1"
        with mock.patch.dict(
            continuation_worker.ACTION_REGISTRY,
            {action: lambda _item: called.append("handler")},
            clear=False,
        ):
            with self.assertRaisesRegex(
                continuation_worker.HumanGate, "sensitive_payload_rejected"
            ):
                continuation_worker.execute(
                    self.item({"connection": "postgresql://user:credential@example/db"})
                )
        self.assertEqual([], called)

    def test_incomplete_or_relative_specs_keep_action_dormant(self) -> None:
        invalid_sets = (
            [],
            [{"path": "/approved/pg_dump", "sha256": "0" * 64}],
            [
                {"path": "relative/pg_dump", "sha256": "0" * 64},
                {"path": "/approved/pg_restore", "sha256": "1" * 64},
            ],
            [
                {"path": "/approved/pg_dump", "sha256": "short"},
                {"path": "/approved/pg_restore", "sha256": "1" * 64},
            ],
        )
        for specs in invalid_sets:
            with mock.patch.object(
                continuation_worker, "_validate_repo",
                return_value=(Path("."), "0" * 40),
            ), mock.patch.object(
                continuation_worker.Path,
                "read_text",
                return_value=json.dumps({"allowed_executable_specs": specs}),
            ):
                with self.assertRaisesRegex(
                    continuation_worker.HumanGate,
                    "executables_not_specifically_approved",
                ):
                    continuation_worker._run_render_postgres_preservation(self.item())

    def test_complete_specs_only_reach_dormant_coordinator(self) -> None:
        specs = [
            {"path": "/approved/pg_dump", "sha256": "0" * 64},
            {"path": "/approved/pg_restore", "sha256": "1" * 64},
        ]
        with mock.patch.object(
            continuation_worker, "_validate_repo",
            return_value=(Path("."), "0" * 40),
        ), mock.patch.object(
            continuation_worker.Path,
            "read_text",
            return_value=json.dumps({"allowed_executable_specs": specs}),
        ), mock.patch.object(continuation_worker, "_run_governed") as governed:
            continuation_worker._run_render_postgres_preservation(self.item())
        governed.assert_called_once()


if __name__ == "__main__":
    unittest.main()

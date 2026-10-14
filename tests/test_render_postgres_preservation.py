from __future__ import annotations

import hashlib
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from scripts import command_gateway, continuation_worker
from scripts.render_postgres_preservation import (
    AccessCleanupFailed,
    PreservationBlocked,
    PreservationPlan,
    preserve,
    reject_sensitive_arguments,
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

    def test_unauthorized_secret_and_wrong_risk_fail_closed(self) -> None:
        unauthorized = self.root / "pg_restore"
        unauthorized.write_bytes(b"unapproved-binary")
        with self.assertRaisesRegex(command_gateway.GatewayError, "não autorizado"):
            command_gateway.validate_command([str(unauthorized)], 1, self.policy)
        with self.assertRaisesRegex(command_gateway.GatewayError, "sensível"):
            command_gateway.validate_command([str(self.tool), "password=leak"], 1, self.policy)
        with self.assertRaisesRegex(command_gateway.GatewayError, "risco 3"):
            command_gateway.validate_command([str(self.tool)], 3, self.policy)


class PreservationCoordinatorTests(unittest.TestCase):
    def plan(self, **overrides: object) -> PreservationPlan:
        values = {
            "enabled": True,
            "approved_credential_channel": True,
            "observed_desktop_ip": "203.0.113.10",
            "requested_allowlist_ip": "203.0.113.10",
            "encrypted_output": Path("/safe/backup.age"),
        }
        values.update(overrides)
        return PreservationPlan(**values)

    def test_ip_mismatch_and_missing_credential_channel_block_before_network(self) -> None:
        for plan in (
            self.plan(requested_allowlist_ip="203.0.113.11"),
            self.plan(approved_credential_channel=False),
        ):
            called: list[str] = []
            with self.assertRaises(PreservationBlocked):
                preserve(
                    plan,
                    add_access=lambda _cidr: called.append("add"),
                    export_encrypted=lambda: called.append("export"),
                    remove_access=lambda _cidr: called.append("remove"),
                    access_is_removed=lambda _cidr: True,
                    validate_offline_restore=lambda: called.append("restore"),
                )
            self.assertEqual([], called)

    def test_access_is_removed_after_export_failure(self) -> None:
        order: list[str] = []
        def fail_export() -> None:
            order.append("export")
            raise RuntimeError("controlled export failure")
        with self.assertRaisesRegex(RuntimeError, "controlled export failure"):
            preserve(
                self.plan(),
                add_access=lambda _cidr: order.append("add"),
                export_encrypted=fail_export,
                remove_access=lambda _cidr: order.append("remove"),
                access_is_removed=lambda _cidr: order.append("readback") or True,
                validate_offline_restore=lambda: order.append("restore"),
            )
        self.assertEqual(["add", "export", "remove", "readback"], order)

    def test_restore_runs_only_after_cleanup_readback(self) -> None:
        order: list[str] = []
        preserve(
            self.plan(),
            add_access=lambda _cidr: order.append("add"),
            export_encrypted=lambda: order.append("export"),
            remove_access=lambda _cidr: order.append("remove"),
            access_is_removed=lambda _cidr: order.append("readback") or True,
            validate_offline_restore=lambda: order.append("restore"),
        )
        self.assertEqual(["add", "export", "remove", "readback", "restore"], order)

    def test_cleanup_failure_is_terminal_and_secret_arguments_are_rejected(self) -> None:
        with self.assertRaises(AccessCleanupFailed):
            preserve(
                self.plan(),
                add_access=lambda _cidr: None,
                export_encrypted=lambda: None,
                remove_access=lambda _cidr: None,
                access_is_removed=lambda _cidr: False,
                validate_offline_restore=lambda: self.fail("restore must not run"),
            )
        with self.assertRaisesRegex(PreservationBlocked, "sensitive_argument"):
            reject_sensitive_arguments(["--dbname=postgresql://user:secret@example/db"])


class DormantActionTests(unittest.TestCase):
    def test_action_remains_disabled_without_exact_binary_specs(self) -> None:
        item = continuation_worker.Continuation(
            request_id="req", idempotency_key="idem", correlation_id="corr",
            project="AI Control Plane", todo={}, attempts=0,
        )
        with mock.patch.object(
            continuation_worker,
            "_validate_repo",
            return_value=(Path("."), "0" * 40),
        ):
            with self.assertRaisesRegex(
                continuation_worker.HumanGate,
                "executables_not_specifically_approved",
            ):
                continuation_worker._run_render_postgres_preservation(item)


if __name__ == "__main__":
    unittest.main()

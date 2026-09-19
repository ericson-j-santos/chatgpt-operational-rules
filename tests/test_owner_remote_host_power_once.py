from __future__ import annotations

import importlib.util
import json
import socket
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

MODULE = SCRIPTS / "owner_remote_host_power_once.py"
spec = importlib.util.spec_from_file_location("owner_remote_host_power_once", MODULE)
assert spec and spec.loader
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class OwnerRemoteHostPowerOnceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / "owner-remote-host-power-once.local.json"
        self.audit = patch.object(m, "append_audit", lambda payload: None)
        self.audit.start()

    def tearDown(self) -> None:
        self.audit.stop()
        self.tmp.cleanup()

    def authorize(self, **overrides):
        kwargs = {
            "action_id": "host-power.remote-reboot-once.desktop.test",
            "executor_host": socket.gethostname(),
            "target_host": "DESKTOP-PDQK954",
            "minutes": 10,
            "confirm": m.AUTHORIZE_CONFIRM,
            "config_path": self.config,
            "authorization_ref": "chat-explicit-desktop-reboot-test",
        }
        kwargs.update(overrides)
        return m.authorize(**kwargs)

    def test_authorization_binds_executor_and_target(self) -> None:
        result = self.authorize()
        self.assertTrue(result["ok"])
        payload = m.load_authorization(
            action_id="host-power.remote-reboot-once.desktop.test",
            config_path=self.config,
        )
        self.assertEqual(payload["executor_host"].casefold(), socket.gethostname().casefold())
        self.assertEqual(payload["target_host"], "DESKTOP-PDQK954")
        self.assertEqual(
            payload["scope"],
            "host://DESKTOP-PDQK954/reboot-once-remote",
        )
        self.assertIsNone(payload["consumed_at"])

    def test_authorize_rejects_local_or_unsafe_target(self) -> None:
        with self.assertRaisesRegex(m.base.HostPowerError, "diferente"):
            self.authorize(target_host=socket.gethostname())
        for target in (r"\\DESKTOP-PDQK954", "DESKTOP/PDQK954", "*"):
            with self.subTest(target=target):
                with self.assertRaisesRegex(m.base.HostPowerError, "target_host"):
                    self.authorize(target_host=target)

    def test_expired_authorization_fails_closed(self) -> None:
        self.authorize(minutes=1)
        payload = json.loads(self.config.read_text(encoding="utf-8"))
        payload["expires_at"] = m.base.utc_iso(
            m.base.utc_now() - timedelta(seconds=1)
        )
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(m.base.HostPowerError, "expirada"):
            m.load_authorization(
                action_id="host-power.remote-reboot-once.desktop.test",
                config_path=self.config,
            )

    def test_execute_consumes_before_submit_and_replay_is_blocked(self) -> None:
        self.authorize()
        observed = {}

        def fake_submit(target_host: str, delay_seconds: int):
            payload = json.loads(self.config.read_text(encoding="utf-8"))
            observed["consumed_before_submit"] = bool(payload["consumed_at"])
            observed["target"] = target_host
            observed["delay"] = delay_seconds
            return subprocess.CompletedProcess(
                ["InitiateSystemShutdownExW", target_host],
                0,
                stdout="accepted",
                stderr="",
            )

        with patch.object(m, "submit_remote_reboot", fake_submit):
            result = m.execute(
                action_id="host-power.remote-reboot-once.desktop.test",
                confirm=m.EXECUTE_CONFIRM,
                config_path=self.config,
                correlation_id="corr-remote-reboot-test",
                delay_seconds=5,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(
            observed,
            {
                "consumed_before_submit": True,
                "target": "DESKTOP-PDQK954",
                "delay": 5,
            },
        )
        with self.assertRaisesRegex(m.base.HostPowerError, "consumida"):
            m.execute(
                action_id="host-power.remote-reboot-once.desktop.test",
                confirm=m.EXECUTE_CONFIRM,
                config_path=self.config,
                correlation_id="corr-remote-reboot-replay",
                delay_seconds=5,
            )

    def test_native_remote_api_uses_exact_unc_target_without_force_close(self) -> None:
        observed = {}

        class FakeAdvapi:
            def InitiateSystemShutdownExW(
                self, machine, message, delay, force_apps_closed,
                reboot_after_shutdown, reason,
            ):
                observed.update(
                    machine=machine,
                    delay=delay,
                    force=force_apps_closed,
                    reboot=reboot_after_shutdown,
                    reason=reason,
                )
                return True

        fake_kernel = object()
        fake_advapi = FakeAdvapi()
        with (
            patch.object(
                m.base,
                "windows_shutdown_api",
                return_value=(fake_kernel, fake_advapi),
            ),
            patch.object(m, "enable_remote_shutdown_privilege") as privilege,
        ):
            result = m.submit_remote_reboot("DESKTOP-PDQK954", 5)

        self.assertEqual(result.returncode, 0)
        privilege.assert_called_once_with(fake_kernel, fake_advapi)
        self.assertEqual(observed["machine"], r"\\DESKTOP-PDQK954")
        self.assertEqual(observed["delay"], 5)
        self.assertFalse(observed["force"])
        self.assertTrue(observed["reboot"])
        self.assertEqual(
            observed["reason"],
            m.base.planned_application_maintenance_reason(),
        )

    def test_route_uses_remote_shutdown_privilege_only(self) -> None:
        source = MODULE.read_text(encoding="utf-8")
        self.assertIn("SeRemoteShutdownPrivilege", source)
        self.assertNotIn('"SeShutdownPrivilege"', source)
        self.assertNotIn("poweroff", source.lower())

    def test_tampered_operation_fails_closed(self) -> None:
        self.authorize()
        payload = json.loads(self.config.read_text(encoding="utf-8"))
        payload["operation"] = "shutdown"
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(m.base.HostPowerError, "somente reboot"):
            m.load_authorization(
                action_id="host-power.remote-reboot-once.desktop.test",
                config_path=self.config,
            )


if __name__ == "__main__":
    unittest.main()

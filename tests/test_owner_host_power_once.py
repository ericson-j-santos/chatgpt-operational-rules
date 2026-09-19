from __future__ import annotations

import importlib.util
import json
import socket
import subprocess
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "owner_host_power_once.py"
spec = importlib.util.spec_from_file_location("owner_host_power_once", MODULE)
assert spec and spec.loader
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class OwnerHostPowerOnceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / "owner-host-power-once.local.json"
        self.audit = patch.object(m, "append_audit", lambda payload: None)
        self.audit.start()

    def tearDown(self) -> None:
        self.audit.stop()
        self.tmp.cleanup()

    def authorize(self, **overrides):
        kwargs = {
            "action_id": "host-power.reboot-once.test",
            "host": socket.gethostname(),
            "minutes": 10,
            "confirm": m.AUTHORIZE_CONFIRM,
            "config_path": self.config,
            "authorization_ref": "chat-explicit-authorization-test",
        }
        kwargs.update(overrides)
        return m.authorize(**kwargs)

    def test_authorization_is_bound_to_current_host_and_owner(self) -> None:
        result = self.authorize()
        self.assertTrue(result["ok"])

        payload = m.load_authorization(
            action_id="host-power.reboot-once.test",
            config_path=self.config,
        )
        self.assertEqual(payload["operation"], "reboot")
        self.assertEqual(
            payload["host"].casefold(), socket.gethostname().casefold()
        )
        self.assertEqual(payload["owner_fingerprint"], m.owner_fingerprint())
        self.assertIsNone(payload["consumed_at"])

    def test_authorize_rejects_other_host(self) -> None:
        with self.assertRaisesRegex(m.HostPowerError, "host local"):
            self.authorize(host="OTHER-HOST")

    def test_authorize_rejects_invalid_window(self) -> None:
        for minutes in (0, m.MAX_WINDOW_MINUTES + 1):
            with self.subTest(minutes=minutes):
                with self.assertRaisesRegex(m.HostPowerError, "janela"):
                    self.authorize(minutes=minutes)

    def test_load_rejects_expired_authorization(self) -> None:
        self.authorize(minutes=1)
        payload = json.loads(self.config.read_text(encoding="utf-8"))
        payload["expires_at"] = m.utc_iso(
            m.utc_now() - timedelta(seconds=1)
        )
        self.config.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaisesRegex(m.HostPowerError, "expirada") as ctx:
            m.load_authorization(
                action_id="host-power.reboot-once.test",
                config_path=self.config,
            )
        self.assertEqual(ctx.exception.exit_code, m.EXIT_EXPIRED)

    def test_execute_consumes_before_submitting_reboot(self) -> None:
        self.authorize()
        observed = {}

        def fake_submit(delay_seconds: int):
            payload = json.loads(self.config.read_text(encoding="utf-8"))
            observed["consumed_before_submit"] = bool(payload["consumed_at"])
            observed["delay"] = delay_seconds
            return subprocess.CompletedProcess(
                ["shutdown.exe"], 0, stdout="scheduled", stderr=""
            )

        with patch.object(m, "submit_reboot", fake_submit):
            result = m.execute(
                action_id="host-power.reboot-once.test",
                confirm=m.EXECUTE_CONFIRM,
                config_path=self.config,
                correlation_id="corr-test-reboot",
                delay_seconds=5,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            observed, {"consumed_before_submit": True, "delay": 5}
        )
        with self.assertRaisesRegex(m.HostPowerError, "consumida"):
            m.execute(
                action_id="host-power.reboot-once.test",
                confirm=m.EXECUTE_CONFIRM,
                config_path=self.config,
                correlation_id="corr-replay",
                delay_seconds=5,
            )

    def test_never_accepts_shutdown_or_poweroff_operation(self) -> None:
        for operation in ("shutdown", "poweroff"):
            with self.subTest(operation=operation):
                self.authorize()
                payload = json.loads(self.config.read_text(encoding="utf-8"))
                payload["operation"] = operation
                payload["consumed_at"] = None
                self.config.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(m.HostPowerError, "somente reboot"):
                    m.load_authorization(
                        action_id="host-power.reboot-once.test",
                        config_path=self.config,
                    )

    def test_revoke_removes_authorization(self) -> None:
        self.authorize()
        self.assertTrue(self.config.exists())
        result = m.revoke(
            confirm=m.REVOKE_CONFIRM,
            config_path=self.config,
        )
        self.assertEqual(result, {"ok": True, "revoked": True})
        self.assertFalse(self.config.exists())

    def test_general_risk3_gateway_still_denies_host_power(self) -> None:
        source = (ROOT / "scripts" / "owner_risk3_gateway.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("HOST_POWER_MARKER_RE", source)
        self.assertIn(
            "reinicialização/desligamento de host permanece bloqueado",
            source,
        )


if __name__ == "__main__":
    unittest.main()

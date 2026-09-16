from __future__ import annotations

import importlib.util
import json
import os
import socket
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "owner_reboot_gateway.py"
INSTALLER = ROOT / "scripts" / "install_owner_reboot_gateway.py"

spec = importlib.util.spec_from_file_location("owner_reboot_gateway", MODULE)
assert spec and spec.loader
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

installer_spec = importlib.util.spec_from_file_location("install_owner_reboot_gateway", INSTALLER)
assert installer_spec and installer_spec.loader
installer = importlib.util.module_from_spec(installer_spec)
installer_spec.loader.exec_module(installer)


class OwnerRebootGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make_fake_reboot(self, *, fail_actual: bool = False) -> tuple[Path, Path]:
        marker = self.root / "reboot-marker.txt"
        script = self.root / "fake_reboot.py"
        script.write_text(
            "from pathlib import Path\n"
            "import sys\n"
            f"marker = Path({str(marker)!r})\n"
            "if '--check' in sys.argv:\n"
            "    print('CHECK_OK')\n"
            "    raise SystemExit(0)\n"
            + (
                "raise SystemExit(7)\n"
                if fail_actual
                else "marker.write_text((marker.read_text() if marker.exists() else '') + 'run\\n', encoding='utf-8')\n"
                "raise SystemExit(0)\n"
            ),
            encoding="utf-8",
        )
        return script, marker

    def make_config(self, reboot_script: Path, **overrides) -> Path:
        host = socket.gethostname()
        payload = {
            "version": 1,
            "enabled": True,
            "action_id": m.ACTION_ID,
            "environment": "local",
            "owner_fingerprint": m.fingerprint(),
            "host": host,
            "scope": f"host://{host}/reboot",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "authorization_id": "auth-test-0001",
            "reboot_script": str(reboot_script),
        }
        payload.update(overrides)
        path = self.root / "owner-risk3-reboot.local.json"
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path

    def test_installer_copies_exact_gateway(self):
        target = self.root / "bin" / "owner_reboot_gateway.py"
        result = installer.atomic_install(MODULE, target)
        self.assertTrue(result["ready"])
        self.assertEqual(target.read_bytes(), MODULE.read_bytes())

    def test_check_does_not_consume_authorization(self):
        script, marker = self.make_fake_reboot()
        config = self.make_config(script)
        audit = self.root / "audit.jsonl"
        self.assertEqual(m.run_gateway(config, audit, check=True), 0)
        persisted = json.loads(config.read_text(encoding="utf-8"))
        self.assertTrue(persisted["enabled"])
        self.assertNotIn("consumed_at", persisted)
        self.assertFalse(marker.exists())

    def test_reboot_authorization_is_one_shot_and_reuse_is_blocked(self):
        script, marker = self.make_fake_reboot()
        config = self.make_config(script)
        audit = self.root / "audit.jsonl"
        self.assertEqual(m.run_gateway(config, audit, check=False), 0)
        persisted = json.loads(config.read_text(encoding="utf-8"))
        self.assertFalse(persisted["enabled"])
        self.assertTrue(persisted["consumed_at"])
        self.assertEqual(persisted["consumed_reason"], "reboot_requested")
        self.assertEqual(marker.read_text(encoding="utf-8"), "run\n")
        self.assertEqual(m.run_gateway(config, audit, check=False), 20)
        self.assertEqual(marker.read_text(encoding="utf-8"), "run\n")

    def test_failed_reboot_still_consumes_authorization_fail_closed(self):
        script, marker = self.make_fake_reboot(fail_actual=True)
        config = self.make_config(script)
        audit = self.root / "audit.jsonl"
        self.assertEqual(m.run_gateway(config, audit, check=False), 20)
        persisted = json.loads(config.read_text(encoding="utf-8"))
        self.assertFalse(persisted["enabled"])
        self.assertTrue(persisted["consumed_at"])
        self.assertFalse(marker.exists())

    def test_missing_authorization_id_is_blocked(self):
        script, _ = self.make_fake_reboot()
        config = self.make_config(script, authorization_id="")
        with self.assertRaisesRegex(RuntimeError, "authorization_id"):
            m.load_config(config)

    def test_host_mismatch_is_blocked(self):
        script, _ = self.make_fake_reboot()
        config = self.make_config(script, host="other-host", scope="host://other-host/reboot")
        with self.assertRaisesRegex(RuntimeError, "host não autorizado"):
            m.load_config(config)

    def test_concurrent_lock_blocks_second_consumer(self):
        script, _ = self.make_fake_reboot()
        config = self.make_config(script)
        audit = self.root / "audit.jsonl"
        lock = config.with_name(config.name + ".lock")
        lock.write_text("busy\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "em uso"):
            m.consume_authorization(config, audit, "auth-test-0001")


if __name__ == "__main__":
    unittest.main()

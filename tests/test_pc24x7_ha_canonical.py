from __future__ import annotations

import unittest
from pathlib import Path

from scripts.ha_database_guard import validate_ha_database

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.pc24x7.yml"
BRIDGE = ROOT / "docker-compose.pc24x7.bridge.yml"
BOOTSTRAP = ROOT / "scripts" / "todo_gateway_pc24x7.py"
SERVICE_HOST = ROOT / "scripts" / "pc24x7_noteri_windows_service.py"
SERVICE_INSTALLER = ROOT / "scripts" / "install_pc24x7_noteri_windows_service_dev.py"
RUNTIME_RULE = ROOT / "rules" / "runtime-routing.md"


class HaCanonicalRuntimeTests(unittest.TestCase):
    def test_ha_guard_accepts_only_canonical_neon_database(self):
        validate_ha_database({
            "HA_MODE": "enabled",
            "DATABASE_URL": "postgresql://user:secret@example.neon.tech/todo_global_bus_dev_ha",
            "HA_DATABASE_REQUIRED_HOST_SUFFIX": ".neon.tech",
            "HA_DATABASE_REQUIRED_NAME": "todo_global_bus_dev_ha",
        })
        for bad in (
            "postgresql://u:p@db:5432/todo_global_bus_dev",
            "postgresql://u:p@127.0.0.1/todo_global_bus_dev_ha",
            "postgresql://u:p@example.neon.tech/todo_global_bus_dev",
        ):
            with self.assertRaises(RuntimeError):
                validate_ha_database({
                    "HA_MODE": "enabled",
                    "DATABASE_URL": bad,
                    "HA_DATABASE_REQUIRED_HOST_SUFFIX": ".neon.tech",
                    "HA_DATABASE_REQUIRED_NAME": "todo_global_bus_dev_ha",
                })

    def test_local_db_and_bridge_are_rollback_profile_only(self):
        compose = COMPOSE.read_text(encoding="utf-8")
        bridge = BRIDGE.read_text(encoding="utf-8")
        self.assertIn('db:\n    profiles: ["rollback"]', compose)
        self.assertIn('db_bridge:\n    profiles: ["rollback"]', bridge)
        gateway_block = compose.split("  gateway:", 1)[1].split("  worker:", 1)[0]
        worker_block = compose.split("  worker:", 1)[1].split("  ingest:", 1)[0]
        self.assertNotIn("condition: service_healthy", gateway_block)
        self.assertNotIn("condition: service_healthy", worker_block)
        self.assertIn('HA_MODE: "enabled"', gateway_block)
        self.assertIn('HA_MODE: "enabled"', worker_block)

    def test_fresh_bootstrap_requires_canonical_neon_secret(self):
        text = BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn('root / "neon-ha.env"', text)
        self.assertIn('canonical DATABASE_URL is not Neon', text)
        self.assertIn('todo_global_bus_dev_ha', text)
        self.assertNotIn('@db:5432/todo_global_bus_dev"', text)

    def test_noteri_service_is_automatic_headless_and_neon_only(self):
        host = SERVICE_HOST.read_text(encoding="utf-8")
        installer = SERVICE_INSTALLER.read_text(encoding="utf-8")
        self.assertIn("StartServiceCtrlDispatcherW", host)
        self.assertIn('"WORKER_ID": "continuation-worker-Noteri-ha-service"', host)
        self.assertIn('"HA_MODE": "enabled"', host)
        self.assertIn("SERVICE_AUTO_START", installer)
        self.assertIn("CreateServiceW", installer)
        self.assertIn('"service_account": "LocalSystem"', installer)
        self.assertIn("continuation-worker-Noteri-ha-service", installer)
        self.assertNotIn("DefaultPassword", installer)
        main_body = installer.split("def main() -> int:", 1)[1]
        self.assertLess(main_body.index("wait_service_heartbeat(values)"), main_body.index("retire_legacy_worker()"))
        self.assertLess(main_body.index("wait_service_heartbeat(values)"), main_body.index("remove_logon_fallback()"))

    def test_runtime_rule_declares_neon_canonical_and_local_rollback_manual(self):
        text = RUNTIME_RULE.read_text(encoding="utf-8")
        self.assertIn("PostgreSQL Neon externo", text)
        self.assertIn("somente rollback manual", text)
        self.assertIn("--profile rollback", text)


if __name__ == "__main__":
    unittest.main()

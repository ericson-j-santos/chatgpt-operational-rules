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
NOTERI_DEPLOY = ROOT / "scripts" / "deploy_pc24x7_noteri_ha_worker_dev.py"
RUNTIME_RULE = ROOT / "rules" / "runtime-routing.md"
NOTERI_CYCLE_OBSERVER = ROOT / "scripts" / "pc24x7_noteri_physical_cycle_desktop_e2e_dev.py"
NOTERI_CYCLE_TRIGGER = ROOT / "scripts" / "pc24x7_physical_cycle_noteri_dev.py"
NOTERI_CYCLE_UAC = ROOT / "scripts" / "request_pc24x7_noteri_reboot_uac_dev.py"


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
        self.assertIn('log_supervisor("worker_retry_wait"', host)
        self.assertIn("RESTART_BASE_SECONDS = 5", host)
        self.assertIn("RESTART_MAX_SECONDS = 60", host)
        self.assertIn("while not _stop_event.is_set()", host)
        self.assertIn('"WORKER_ID": "continuation-worker-Noteri-ha-service"', host)
        self.assertIn('"HA_MODE": "enabled"', host)
        self.assertIn("SERVICE_AUTO_START", installer)
        self.assertIn("SERVICE_CONFIG_DELAYED_AUTO_START_INFO", installer)
        self.assertIn("enable_delayed_auto_start(service)", installer)
        self.assertIn('"start_type": "automatic_delayed"', installer)
        self.assertIn("CreateServiceW", installer)
        self.assertIn('"service_account": "LocalSystem"', installer)
        self.assertIn("continuation-worker-Noteri-ha-service", installer)
        self.assertNotIn("DefaultPassword", installer)
        main_body = installer.split("def main() -> int:", 1)[1]
        self.assertLess(main_body.index("wait_service_heartbeat(values)"), main_body.index("retire_legacy_worker()"))
        self.assertLess(main_body.index("wait_service_heartbeat(values)"), main_body.index("remove_logon_fallback()"))

    def test_noteri_legacy_launcher_also_enforces_neon_only(self):
        text = NOTERI_DEPLOY.read_text(encoding="utf-8")
        self.assertIn('"HA_MODE": "enabled"', text)
        self.assertIn('"HA_DATABASE_REQUIRED_HOST_SUFFIX": ".neon.tech"', text)
        self.assertIn('"HA_DATABASE_REQUIRED_NAME": "todo_global_bus_dev_ha"', text)

    def test_runtime_rule_declares_neon_canonical_and_local_rollback_manual(self):
        text = RUNTIME_RULE.read_text(encoding="utf-8")
        self.assertIn("PostgreSQL Neon externo", text)
        self.assertIn("somente rollback manual", text)
        self.assertIn("--profile rollback", text)


if __name__ == "__main__":
    unittest.main()


def test_noteri_physical_cycle_is_guarded_and_symmetric():
    observer = NOTERI_CYCLE_OBSERVER.read_text(encoding="utf-8")
    trigger = NOTERI_CYCLE_TRIGGER.read_text(encoding="utf-8")
    uac = NOTERI_CYCLE_UAC.read_text(encoding="utf-8")
    self = unittest.TestCase()
    self.assertIn('DESKTOP = "DESKTOP-PDQK954"', observer)
    self.assertIn('NOTERI = "Noteri"', observer)
    self.assertIn('NOTERI_SERVICE_WORKER = "continuation-worker-Noteri-ha-service"', observer)
    self.assertIn('assert_execution(desktop_survival, DESKTOP)', observer)
    self.assertIn('assert_execution(noteri_takeover, NOTERI)', observer)
    self.assertIn('finally:', observer)
    self.assertIn('docker("start", DESKTOP_WORKER_CONTAINER, check=False)', observer)
    self.assertIn('TARGET = "Noteri"', trigger)
    self.assertIn("InitiateSystemShutdownExW", trigger)
    self.assertIn("SeShutdownPrivilege", trigger)
    self.assertIn('refusing physical cycle on unexpected host', trigger)
    self.assertNotIn('"shutdown.exe"', trigger)
    self.assertIn('lpVerb = "runas"', uac)
    self.assertIn('pc24x7_physical_cycle_noteri_dev.py', uac)

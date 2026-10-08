"""Contrato do relatório de onboarding: positivo, negativo, replay e E2E isolado."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import host_onboarding_report as onboarding  # noqa: E402
from host_readiness import ReadinessError  # noqa: E402


def inventory() -> dict:
    return {
        "schema_version": 1,
        "hostname": "TEST-HOST",
        "os": "Windows",
        "architecture": "AMD64",
        "logical_cpus": 6,
        "memory_gib": 16.0,
        "disk_total_gib": 256.0,
        "disk_free_gib": 128.0,
        "tools_detected": {"python": True, "git": True, "docker": False},
    }


class HostOnboardingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = json.loads(
            (ROOT / "config" / "host-readiness.json").read_text(encoding="utf-8")
        )

    def test_inventory_only_never_authorizes_install_or_work(self):
        report = onboarding.build_report(self.contract, "desktop-pc24x7", inventory())
        self.assertEqual(report["compatibility"]["status"], "needs_governed_capability_evidence")
        self.assertFalse(report["ready_for_work"])
        self.assertFalse(report["install_authorized"])
        self.assertIn("command_gateway", report["compatibility"]["unconfirmed_capabilities"])
        self.assertIn("docker_cli_not_detected_not_necessarily_required", report["compatibility"]["observations"])

    def test_declared_all_capabilities_still_requires_physical_e2e(self):
        evidence = {
            "profile": "desktop-pc24x7",
            "capabilities": {
                name: True
                for name in self.contract["hosts"]["desktop-pc24x7"]["required_capabilities"]
            },
        }
        result = onboarding.build_report(self.contract, "desktop-pc24x7", inventory(), evidence, "a" * 40)
        self.assertEqual(result["compatibility"]["status"], "awaiting_physical_e2e")
        self.assertEqual(result["source_sha"], "a" * 40)
        self.assertFalse(result["ready_for_work"])

    def test_missing_capability_remains_blocked(self):
        report = onboarding.build_report(
            self.contract, "desktop-pc24x7", inventory(),
            {"profile": "desktop-pc24x7", "capabilities": {"command_gateway": True}},
        )
        self.assertEqual(report["compatibility"]["status"], "missing_required_capabilities")
        self.assertIn("restart_recovery", report["compatibility"]["unconfirmed_capabilities"])

    def test_reject_sensitive_or_unknown_inventory_fields(self):
        untrusted = inventory()
        untrusted["access_token"] = "never-output-this"
        with self.assertRaisesRegex(ReadinessError, "inventory_schema_mismatch"):
            onboarding.build_report(self.contract, "desktop-pc24x7", untrusted)

    def test_reject_invalid_hardware_and_source_revision(self):
        bad = inventory()
        bad["logical_cpus"] = True
        with self.assertRaisesRegex(ReadinessError, "inventory_logical_cpus_invalid"):
            onboarding.build_report(self.contract, "noteri", bad)
        bad = inventory()
        bad["disk_free_gib"] = 300
        with self.assertRaisesRegex(ReadinessError, "inventory_disk_inconsistent"):
            onboarding.build_report(self.contract, "noteri", bad)
        with self.assertRaisesRegex(ReadinessError, "source_sha_invalid"):
            onboarding.build_report(self.contract, "noteri", inventory(), source_sha="main")

    def test_local_collection_is_schema_only_and_no_subprocess(self):
        inv = onboarding.collect_local()
        self.assertEqual(set(inv), onboarding.INVENTORY_KEYS)
        self.assertEqual(set(inv["tools_detected"]), set(onboarding.TOOLS))
        onboarding.validate_inventory(inv)

    def test_e2e_external_inventory_replay_and_negative_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            fixture = p / "inventory.json"
            output = p / "compatibility.json"
            fixture.write_text(json.dumps(inventory()), encoding="utf-8")
            cmd = [
                sys.executable, str(ROOT / "scripts" / "host_onboarding_report.py"),
                "--profile", "desktop-pc24x7", "--inventory", str(fixture),
                "--source-sha", "b" * 40, "--output", str(output),
            ]
            first = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
            self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
            stored = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(stored["source_sha"], "b" * 40)
            self.assertFalse(stored["ready_for_work"])
            again = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
            self.assertEqual(again.returncode, 0, again.stderr + again.stdout)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), stored)
            changed = inventory()
            changed["logical_cpus"] = 12
            fixture.write_text(json.dumps(changed), encoding="utf-8")
            rejected = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
            self.assertEqual(rejected.returncode, 2)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), stored)

    def test_e2e_bad_inventory_does_not_create_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            fixture = p / "untrusted.json"
            output = p / "should-not-exist.json"
            fixture.write_text(json.dumps({**inventory(), "private_key": "secret"}), encoding="utf-8")
            proc = subprocess.run([
                sys.executable, str(ROOT / "scripts" / "host_onboarding_report.py"),
                "--profile", "noteri", "--inventory", str(fixture), "--output", str(output),
            ], capture_output=True, text=True, timeout=20, check=False)
            self.assertEqual(proc.returncode, 2)
            self.assertFalse(output.exists())
            self.assertNotIn("secret", proc.stdout)


if __name__ == "__main__":
    unittest.main()

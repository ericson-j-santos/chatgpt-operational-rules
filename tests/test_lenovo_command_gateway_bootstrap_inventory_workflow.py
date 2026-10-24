"""Contract tests for the one-time TODO-22 Lenovo bootstrap workflow."""
from __future__ import annotations

import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "lenovo-command-gateway-bootstrap-inventory.yml"


class LenovoBootstrapInventoryWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def test_trigger_is_single_owner_draft_body_transition(self) -> None:
        text = self.text
        self.assertIn("types: [edited]", text)
        self.assertNotIn("types: [synchronize]", text)
        self.assertNotIn("\n  push:", text)
        self.assertNotIn("\n  schedule:", text)
        self.assertIn("github.actor == github.repository_owner", text)
        self.assertIn("github.event.pull_request.draft == true", text)
        self.assertIn("TODO22_LENOVO_GATEWAY_BOOTSTRAP_ONCE_20261024", text)
        self.assertIn("github.event.changes.body.from", text)
        self.assertIn("cancel-in-progress: false", text)

    def test_exact_lenovo_runner_and_fixed_bootstrap_identity(self) -> None:
        text = self.text
        self.assertIn("desktop-rp23ogs]", text)
        self.assertIn("DESKTOP-RP23OGS-runtime", text)
        self.assertIn("$env:COMPUTERNAME -ne 'DESKTOP-RP23OGS'", text)
        self.assertIn("'--commit', $env:EXPECTED_HEAD", text)
        self.assertIn(
            "73ef3c091a16a261dfe71fa0ac7bb2e2c6a7f07bdfa3bc592c44855751c92329",
            text,
        )
        self.assertIn("INSTALLER_PIN_MISMATCH", text)
        self.assertIn("HOST_BOOTSTRAP_READBACK_MISMATCH", text)

    def test_session_gateway_and_read_only_probe_are_mandatory(self) -> None:
        text = self.text
        self.assertIn("bin\\session_launcher.py", text)
        self.assertIn("'--require-runner-version-preflight'", text)
        self.assertIn("bin\\command_gateway.py", text)
        self.assertIn("'--risk', '1'", text)
        self.assertIn("scripts/native_host_capacity_probe.py", text)
        self.assertIn("capacity_sample_complete -ne $true", text)
        self.assertIn("ready_for_migration -ne $false", text)
        self.assertIn("state_changed -ne $false", text)

    def test_no_todo_install_service_restart_or_secret_surface(self) -> None:
        lower = self.text.lower()
        for forbidden in (
            "install_todo_gateway_debian_systemd.py",
            "systemctl",
            "restart-service",
            "restart-computer",
            "shutdown",
            "format-volume",
            "secrets.",
            "password",
            "token:",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, lower)
        self.assertIn("todo_gateway_installed = $false", self.text)
        self.assertIn("services_restarted = $false", self.text)
        self.assertIn("rebooted = $false", self.text)

    def test_hosted_admission_watchdog_and_sanitized_artifact(self) -> None:
        text = self.text
        self.assertIn("Lenovo bootstrap immutable admission", text)
        self.assertIn("Rules, Gateway, Session Bootstrap and E2E gate", text)
        self.assertIn("Command Gateway Windows policy compatibility", text)
        self.assertIn("Debian installer isolated systemd homologation", text)
        self.assertIn("SELF_HOSTED_RUNNER_UNAVAILABLE:300s", text)
        self.assertIn("cancelWorkflowRun", text)
        self.assertIn("actions/upload-artifact@v7", text)
        self.assertIn("todo22-lenovo-bootstrap-inventory-evidence.json", text)
        self.assertNotIn("bootstrapText | Set-Content", text)
        self.assertNotIn("gatewayError |", text)


if __name__ == "__main__":
    unittest.main()

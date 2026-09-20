from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rdc_owner_arbitration as roa


class RdcOwnerArbitrationTests(unittest.TestCase):
    def test_launcher_v5_delegates_to_ready_claim_supervisor(self) -> None:
        self.assertIn(roa.V5_MARKER, roa.LAUNCHER_V5)
        self.assertIn("rdc-interactive-supervisor.ps1", roa.LAUNCHER_V5)
        self.assertIn("--self-test", roa.LAUNCHER_V5.casefold())
        self.assertNotIn(" npx ", roa.LAUNCHER_V5.casefold())

    def test_interactive_supervisor_uses_fresh_claim_not_task_acl(self) -> None:
        text = roa.SUPERVISOR_PS1
        self.assertIn(roa.PS1_MARKER, text)
        self.assertIn("rdc-headless-owner.json", text)
        self.assertIn("$ClaimMaxAgeSeconds = 8", text)
        self.assertIn('return "error"', text)
        self.assertIn("interactive_yield", text)
        self.assertNotIn("Schedule.Service", text)
        self.assertNotIn("RemoteDesktopCommanderHeadless", text)

    def test_headless_claim_requires_transport_proof_and_clears_on_exit(self) -> None:
        text = roa.HEADLESS_RUNNER_V4
        self.assertIn(roa.HEADLESS_MARKER, text)
        presence_probe = text.index("Presence tracked")
        ready_probe = text.index("Device ready:")
        claim_call = text.index("writeClaim(child.pid)")
        self.assertLess(presence_probe, ready_probe)
        self.assertLess(ready_probe, claim_call)
        self.assertIn("presenceTracked", text)
        self.assertIn("transport_proven=true", text)
        self.assertIn("setInterval", text)
        self.assertIn("clearClaim();", text)
        self.assertIn("child.on('exit'", text)

    def test_headless_transport_failure_revokes_claim_and_restarts_child(self) -> None:
        text = roa.HEADLESS_RUNNER_V4
        for marker in (
            "Failed to update transport capability:",
            "Channel subscription timed out",
            "Channel error:",
            "Channel closed",
        ):
            self.assertIn(marker, text)
        failure_probe = text.index("Failed to update transport capability:")
        claim_call = text.index("writeClaim(child.pid)")
        self.assertLess(failure_probe, claim_call)
        self.assertIn("revokeTransport", text)
        self.assertIn("transport_guard revoke=true", text)
        self.assertIn("child.kill()", text)
        self.assertIn("clearClaim();", text)

    def test_apply_migrates_v4_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            launcher = root / "start-remote-desktop-commander.cmd"
            supervisor = root / "rdc-interactive-supervisor.ps1"
            headless = root / "rdc-headless-runner.cjs"
            backups = root / "backups"
            launcher.write_text(
                "@echo off\n" + roa.V4_MARKER + "\n"
                "powershell.exe -File rdc-interactive-supervisor.ps1\n",
                encoding="utf-8",
            )
            supervisor.write_text(
                "# RDC_INTERACTIVE_OWNER_SUPERVISOR_V1\n",
                encoding="utf-8",
            )
            headless.write_text(
                roa.HEADLESS_MARKER_V3 + "\n"
                "const { spawn } = require('child_process');\n"
                "spawn(process.execPath, ['C:/desktop-commander/dist/index.js', 'remote']);\n",
                encoding="utf-8",
            )

            result = roa.apply(launcher, supervisor, headless, backups)
            self.assertEqual(result["result"], "READY_CLAIM_ARBITRATION_APPLIED")
            state = roa.inspect(launcher, supervisor, headless)
            self.assertEqual(state["launcher_marker"], "v5")
            self.assertTrue(state["supervisor_marker"])
            self.assertTrue(state["headless_marker"])
            self.assertIn(roa.HEADLESS_MARKER, headless.read_text(encoding="utf-8"))
            self.assertEqual(len(result["backups"]), 3)

            replay = roa.apply(launcher, supervisor, headless, backups)
            self.assertEqual(replay["result"], "already_applied")

    def test_rejects_unrecognized_headless_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            launcher = root / "start-remote-desktop-commander.cmd"
            supervisor = root / "rdc-interactive-supervisor.ps1"
            headless = root / "rdc-headless-runner.cjs"
            launcher.write_text("@echo off\n" + roa.V4_MARKER + "\n", encoding="utf-8")
            headless.write_text("console.log('unknown');\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "headless runner structure"):
                roa.apply(launcher, supervisor, headless, root / "backups")


if __name__ == "__main__":
    unittest.main()
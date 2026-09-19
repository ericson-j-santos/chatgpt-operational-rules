from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rdc_owner_arbitration as roa


class RdcOwnerArbitrationTests(unittest.TestCase):
    def test_launcher_v4_delegates_to_supervisor(self) -> None:
        self.assertIn(roa.V4_MARKER, roa.LAUNCHER_V4)
        self.assertIn("rdc-interactive-supervisor.ps1", roa.LAUNCHER_V4)
        self.assertIn("--self-test", roa.LAUNCHER_V4.casefold())
        self.assertNotIn(" npx ", roa.LAUNCHER_V4.casefold())

    def test_interactive_supervisor_is_fail_closed_and_yields(self) -> None:
        text = roa.SUPERVISOR_PS1
        self.assertIn(roa.PS1_MARKER, text)
        self.assertIn('return "error"', text)
        self.assertIn('if ($state -eq "error")', text)
        self.assertIn('interactive_yield', text)
        self.assertIn('taskkill.exe', text)
        self.assertIn(roa.PINNED_PACKAGE, text)

    def test_headless_runner_claims_before_child_and_restarts_forever(self) -> None:
        text = roa.HEADLESS_RUNNER_V2
        self.assertIn(roa.HEADLESS_MARKER, text)
        claim = text.index("headless_claim grace_seconds=5")
        delay = text.index("await delay(5000)")
        child = text.index("child_start")
        loop = text.index("while (true)")
        retry = text.index("await delay(15000)")
        self.assertLess(claim, delay)
        self.assertLess(delay, child)
        self.assertLess(loop, child)
        self.assertGreater(retry, child)

    def test_apply_migrates_governed_v3_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            launcher = root / "start-remote-desktop-commander.cmd"
            supervisor = root / "rdc-interactive-supervisor.ps1"
            headless = root / "rdc-headless-runner.cjs"
            backups = root / "backups"
            launcher.write_text(
                "@echo off\n" + roa.V3_MARKER + "\ncall npx --yes "
                + roa.PINNED_PACKAGE + " remote\ngoto :run\n",
                encoding="utf-8",
            )
            headless.write_text(
                "const { spawn } = require('child_process');\n"
                "spawn(process.execPath, ['C:/desktop-commander/dist/index.js', 'remote']);\n",
                encoding="utf-8",
            )

            result = roa.apply(launcher, supervisor, headless, backups)
            self.assertEqual(result["result"], "OWNER_ARBITRATION_APPLIED")
            self.assertEqual(roa.inspect(launcher, supervisor, headless)["launcher_marker"], "v4")
            self.assertTrue(roa.inspect(launcher, supervisor, headless)["supervisor_marker"])
            self.assertTrue(roa.inspect(launcher, supervisor, headless)["headless_marker"])
            self.assertEqual(len(result["backups"]), 2)

            replay = roa.apply(launcher, supervisor, headless, backups)
            self.assertEqual(replay["result"], "already_applied")

    def test_rejects_unrecognized_headless_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            launcher = root / "start-remote-desktop-commander.cmd"
            supervisor = root / "rdc-interactive-supervisor.ps1"
            headless = root / "rdc-headless-runner.cjs"
            launcher.write_text("@echo off\n" + roa.V3_MARKER + "\n", encoding="utf-8")
            headless.write_text("console.log('unknown');\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "headless runner structure"):
                roa.apply(launcher, supervisor, headless, root / "backups")


if __name__ == "__main__":
    unittest.main()
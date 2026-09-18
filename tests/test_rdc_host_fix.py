from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rdc_host_fix as rhf


class RdcHostFixTests(unittest.TestCase):
    def test_v3_launcher_pins_release_and_never_exits_after_child(self) -> None:
        text = rhf.NEW_CONTENT
        self.assertIn(rhf.V3_MARKER, text)
        self.assertIn("@wonderwhy-er/desktop-commander@0.2.51", text)
        self.assertIn("call npx --yes %RDC_PACKAGE% remote", text)
        self.assertIn("goto :run", text)
        self.assertNotIn('if "%RDC_EXIT%"=="0" exit /b 0', text)

    def test_apply_migrates_allowed_source_with_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / rhf.ALLOWED_NAME
            original = ("@echo off\r\n" + rhf.V2_MARKER + "\r\n").encode("utf-8")
            path.write_bytes(original)
            digest = hashlib.sha256(original).hexdigest()
            result = rhf.apply(path, [digest])
            self.assertEqual(result["result"], "fixed")
            self.assertTrue(Path(str(result["backup"])).is_file())
            state = rhf.inspect(path)
            self.assertEqual(state["marker"], "v3")
            self.assertTrue(state["contains_pinned_package"])
            self.assertTrue(state["contains_watchdog_loop"])

    def test_rejects_unexpected_source_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / rhf.ALLOWED_NAME
            path.write_text("@echo off\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "launcher hash changed"):
                rhf.apply(path, ["0" * 64])


if __name__ == "__main__":
    unittest.main()
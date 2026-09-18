from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.host_operating_profile import load_profile, write_profile


class HostOperatingProfileTests(unittest.TestCase):
    def test_missing_file_defaults_to_normal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "host-profile.json"
            result = load_profile(path)
            self.assertEqual(result["profile"], "NORMAL")
            self.assertTrue(result["accepts_new_development"])
            self.assertEqual(result["source"], "default")

    def test_write_estudo_and_read_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "host-profile.json"
            written = write_profile(
                path,
                host="Noteri",
                profile="ESTUDO",
                correlation_id="corr-study-mode-001",
            )
            loaded = load_profile(path)
            self.assertEqual(written["profile"], "ESTUDO")
            self.assertEqual(loaded["profile"], "ESTUDO")
            self.assertFalse(loaded["accepts_new_development"])
            self.assertEqual(loaded["host"], "Noteri")
            self.assertFalse(any(path.parent.glob("*.tmp")))

    def test_invalid_profile_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "host-profile.json"
            path.write_text(json.dumps({"profile": "INVALID"}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "NORMAL ou ESTUDO"):
                load_profile(path)

    def test_short_correlation_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "host-profile.json"
            with self.assertRaisesRegex(ValueError, "8..128"):
                write_profile(path, host="Noteri", profile="NORMAL", correlation_id="x")


if __name__ == "__main__":
    unittest.main()

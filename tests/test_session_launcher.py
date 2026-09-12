from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import command_gateway as cg
import session_bootstrap as sb
import session_launcher as sl


class SessionLauncherTests(unittest.TestCase):
    def test_parse_version(self) -> None:
        self.assertEqual(sl.parse_version("1.6.0"), (1, 6, 0))
        with self.assertRaises(cg.GatewayError):
            sl.parse_version("1.6")

    def test_minimum_version_is_enforced(self) -> None:
        self.assertEqual(sl.ensure_min_version({"rules_version": "1.5.2"}), "1.5.2")
        with self.assertRaises(cg.GatewayError) as ctx:
            sl.ensure_min_version({"rules_version": "1.5.1"})
        self.assertEqual(ctx.exception.exit_code, sl.EXIT_SESSION_LAUNCHER)

    def test_generated_session_id_is_valid(self) -> None:
        value = sl.generate_session_id(Path("C:/dev/chatgpt-workers/reqsys-main-canonical"), "ReqSys")
        self.assertLessEqual(len(value), 64)
        self.assertEqual(sb.validate_session_id(value), value)
        self.assertTrue(value.startswith("reqsys-"))

    def test_expected_head_requires_full_sha(self) -> None:
        head = "a" * 40
        self.assertEqual(sl.validate_expected_head(head.upper()), head)
        with self.assertRaises(cg.GatewayError):
            sl.validate_expected_head("abc123")


if __name__ == "__main__":
    unittest.main()

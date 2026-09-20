from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import tool_router as tr


class ToolRouterTests(unittest.TestCase):
    def test_github_uses_native_api_even_with_other_tools_available(self) -> None:
        result = tr.evaluate({
            "task_type": "github",
            "capabilities": {"github_api": True, "tinyfish": True, "remote_desktop": True},
            "tinyfish_balance_usd": 10,
            "remote_calls_left_pct": 90,
        })
        self.assertTrue(result["ready"])
        self.assertEqual(result["selected_executor"], "github_api")

    def test_web_research_prefers_native_web(self) -> None:
        result = tr.evaluate({
            "task_type": "web_research",
            "capabilities": {"native_web": True, "tinyfish": True},
            "tinyfish_balance_usd": 50,
        })
        self.assertEqual(result["selected_executor"], "native_web")
        self.assertIn("tinyfish", result["avoided"])

    def test_browser_prefers_cloud_browser_before_tinyfish(self) -> None:
        result = tr.evaluate({
            "task_type": "browser_automation",
            "capabilities": {"cloud_browser": True, "tinyfish": True},
            "tinyfish_balance_usd": 50,
        })
        self.assertEqual(result["selected_executor"], "cloud_browser")

    def test_tinyfish_is_blocked_when_balance_is_nonpositive(self) -> None:
        result = tr.evaluate({
            "task_type": "web_research",
            "capabilities": {"native_web": False, "tinyfish": True},
            "tinyfish_balance_usd": -2.36,
        })
        self.assertFalse(result["ready"])
        self.assertIsNone(result["selected_executor"])
        self.assertIn("tinyfish_balance_nonpositive", result["avoided"])

    def test_tinyfish_can_be_contingency_with_positive_balance(self) -> None:
        result = tr.evaluate({
            "task_type": "web_research",
            "capabilities": {"native_web": False, "tinyfish": True},
            "tinyfish_balance_usd": 1.0,
        })
        self.assertEqual(result["selected_executor"], "tinyfish")

    def test_remote_reserve_mode_still_allows_intrinsically_local_task(self) -> None:
        result = tr.evaluate({
            "task_type": "local_machine",
            "capabilities": {"remote_desktop": True},
            "remote_controller_online": True,
            "remote_calls_left_pct": 16,
        })
        self.assertTrue(result["reserve_mode"])
        self.assertEqual(result["selected_executor"], "remote_desktop")
        self.assertIn("remote_reserve_mode_allowed_for_local_task", result["reasons"])

    def test_remote_is_not_used_for_nonlocal_task_in_reserve_mode(self) -> None:
        result = tr.evaluate({
            "task_type": "github",
            "capabilities": {"github_api": True, "remote_desktop": True},
            "remote_calls_left_pct": 16,
        })
        self.assertEqual(result["selected_executor"], "github_api")
        self.assertIn("remote_desktop_reserve_mode", result["avoided"])

    def test_local_task_without_controller_fails_closed(self) -> None:
        result = tr.evaluate({
            "task_type": "host_recovery",
            "capabilities": {"remote_desktop": True},
            "remote_controller_online": False,
            "remote_calls_left_pct": 16,
        })
        self.assertFalse(result["ready"])
        self.assertEqual(result["next_step"], "dual_host_preflight_or_controller_recovery")

    def test_invalid_remote_percentage_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            tr.evaluate({
                "task_type": "local_machine",
                "capabilities": {"remote_desktop": True},
                "remote_controller_online": True,
                "remote_calls_left_pct": 101,
            })


if __name__ == "__main__":
    unittest.main()

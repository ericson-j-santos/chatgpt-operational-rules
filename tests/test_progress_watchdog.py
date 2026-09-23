from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import progress_watchdog as pw


BASE = {
    "task_id": "task-1",
    "correlation_id": "corr-1",
    "state": "running",
    "now_at": "2026-09-22T20:30:00-03:00",
    "last_material_progress_at": "2026-09-22T20:25:01-03:00",
}


class ProgressWatchdogTests(unittest.TestCase):
    def test_within_window_continues(self) -> None:
        result = pw.evaluate(BASE)
        self.assertFalse(result["stalled"])
        self.assertEqual(result["decision"], "continue")

    def test_exact_five_minute_boundary_is_stalled(self) -> None:
        payload = {
            **BASE,
            "last_material_progress_at": "2026-09-22T20:25:00-03:00",
        }
        result = pw.evaluate(payload)
        self.assertTrue(result["stalled"])
        self.assertEqual(result["decision"], "block")
        self.assertEqual(result["stall_after_seconds"], 300)
        self.assertEqual(result["no_progress_seconds"], 300)

    def test_recent_heartbeat_does_not_mask_old_material_progress(self) -> None:
        payload = {
            **BASE,
            "last_material_progress_at": "2026-09-22T20:00:00-03:00",
            "last_observation_at": "2026-09-22T20:29:59-03:00",
        }
        result = pw.evaluate(payload)
        self.assertTrue(result["stalled"])
        self.assertEqual(result["decision"], "block")
        self.assertEqual(result["observation_age_seconds"], 1)
        self.assertEqual(result["no_progress_seconds"], 1800)

    def test_stalled_with_alternative_switches_route(self) -> None:
        result = pw.evaluate({
            **BASE,
            "last_material_progress_at": "2026-09-22T20:00:00-03:00",
            "alternative_route_available": True,
        })
        self.assertEqual(result["decision"], "switch_route")
        self.assertEqual(
            result["reason_code"],
            "material_progress_timeout_alternative_available",
        )

    def test_stalled_without_alternative_blocks(self) -> None:
        result = pw.evaluate({
            **BASE,
            "last_material_progress_at": "2026-09-22T20:00:00-03:00",
            "alternative_route_available": False,
        })
        self.assertEqual(result["decision"], "block")

    def test_terminal_state_does_not_reopen(self) -> None:
        result = pw.evaluate({
            **BASE,
            "state": "completed",
            "last_material_progress_at": "2026-09-22T18:00:00-03:00",
            "alternative_route_available": True,
        })
        self.assertFalse(result["stalled"])
        self.assertEqual(result["decision"], "terminal")

    def test_naive_timestamp_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            pw.evaluate({
                **BASE,
                "last_material_progress_at": "2026-09-22T20:00:00",
            })

    def test_future_progress_timestamp_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            pw.evaluate({
                **BASE,
                "last_material_progress_at": "2026-09-22T21:00:00-03:00",
            })


if __name__ == "__main__":
    unittest.main()

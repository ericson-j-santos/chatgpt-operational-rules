from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "runner_version_preflight.py"
SPEC = importlib.util.spec_from_file_location("runner_version_preflight", MODULE)
assert SPEC and SPEC.loader
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


class RunnerVersionPreflightTests(unittest.TestCase):
    def test_version_parser_accepts_listener_output(self) -> None:
        self.assertEqual(str(m.RunnerVersion.parse("2.337.0\n")), "2.337.0")

    def test_registration_minimum_is_fail_closed(self) -> None:
        result = m.evaluate_policy(
            installed=m.RunnerVersion.parse("2.328.0"),
            deprecation=None,
            deprecation_status="http_403",
            latest_release={"tag_name": "v2.337.0", "published_at": "2026-08-26T14:33:29Z"},
            latest_status="ok",
            now=datetime(2026, 9, 28, tzinfo=timezone.utc),
        )
        self.assertFalse(result["ok"])
        self.assertFalse(result["registration_supported"])
        self.assertTrue(result["runtime_supported_now"])

    def test_current_pickup_does_not_hide_expired_runtime_deadline(self) -> None:
        result = m.evaluate_policy(
            installed=m.RunnerVersion.parse("2.330.0"),
            deprecation={
                "runner_version": "2.330.0",
                "runtime_deprecates_at": "2026-09-01T00:00:00Z",
                "registration_deprecates_at": "2026-10-01T00:00:00Z",
            },
            deprecation_status="ok",
            latest_release={"tag_name": "v2.337.0", "published_at": "2026-08-26T14:33:29Z"},
            latest_status="ok",
            now=datetime(2026, 9, 28, tzinfo=timezone.utc),
        )
        self.assertFalse(result["ok"])
        self.assertFalse(result["runtime_supported_now"])
        self.assertTrue(result["registration_supported"])

    def test_api_permission_failure_is_not_misclassified_as_deprecation(self) -> None:
        result = m.evaluate_policy(
            installed=m.RunnerVersion.parse("2.337.0"),
            deprecation=None,
            deprecation_status="http_403",
            latest_release={"tag_name": "v2.337.0", "published_at": "2026-08-26T14:33:29Z"},
            latest_status="ok",
            now=datetime(2026, 9, 28, tzinfo=timezone.utc),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["deprecation_api_status"], "http_403")
        self.assertEqual(result["runtime_support_evidence"], "current_job_pickup")

    def test_public_latest_is_advisory_not_a_false_block(self) -> None:
        result = m.evaluate_policy(
            installed=m.RunnerVersion.parse("2.336.0"),
            deprecation={"runner_version": "2.336.0"},
            deprecation_status="ok",
            latest_release={"tag_name": "v2.337.0", "published_at": "2026-08-26T14:33:29Z"},
            latest_status="ok",
            now=datetime(2026, 9, 28, tzinfo=timezone.utc),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["latest_public_comparison"], "behind")
        self.assertTrue(result["latest_public_is_advisory"])

    def test_registration_deadline_overrides_numeric_minimum(self) -> None:
        result = m.evaluate_policy(
            installed=m.RunnerVersion.parse("2.336.0"),
            deprecation={
                "runner_version": "2.336.0",
                "registration_deprecates_at": "2026-09-27T00:00:00Z",
            },
            deprecation_status="ok",
            latest_release=None,
            latest_status="unavailable",
            now=datetime(2026, 9, 28, tzinfo=timezone.utc),
        )
        self.assertFalse(result["ok"])
        self.assertFalse(result["registration_supported"])


if __name__ == "__main__":
    unittest.main()

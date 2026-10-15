from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "runner_version_preflight.py"
SPEC = importlib.util.spec_from_file_location("runner_version_preflight", MODULE)
assert SPEC and SPEC.loader
m = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = m
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


    def test_native_file_version_is_parsed_from_fixed_resource(self) -> None:
        fields = [0xFEEF04BD, 0x10000, (2 << 16) | 337, 0] + [0] * 9
        self.assertEqual(m.version_from_fixed_file_info(fields), m.RunnerVersion(2, 337, 0))

    def test_invalid_resource_signature_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "runner_version_fixed_info_invalid"):
            m.version_from_fixed_file_info([0] * 13)

    def test_truncated_resource_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "runner_version_fixed_info_invalid"):
            m.version_from_fixed_file_info([0xFEEF04BD])

    def test_only_winerror_4551_can_select_windows_metadata(self) -> None:
        denied = OSError("application control")
        denied.winerror = 4551
        metadata = m.RunnerVersion.parse("2.337.0")
        with (
            mock.patch.object(m.Path, "is_file", return_value=True),
            mock.patch.object(m.subprocess, "run", side_effect=denied),
            mock.patch.object(m, "read_windows_file_version", return_value=metadata) as fallback,
        ):
            self.assertEqual(m.detect_runner_version(Path("runner")), metadata)
            fallback.assert_called_once()

    def test_other_os_error_never_selects_metadata(self) -> None:
        error = OSError("not app control")
        error.winerror = 5
        with (
            mock.patch.object(m.Path, "is_file", return_value=True),
            mock.patch.object(m.subprocess, "run", side_effect=error),
            mock.patch.object(m, "read_windows_file_version") as fallback,
        ):
            with self.assertRaises(OSError):
                m.detect_runner_version(Path("runner"))
            fallback.assert_not_called()

    def test_failed_process_exit_cannot_use_fallback(self) -> None:
        with (
            mock.patch.object(m.Path, "is_file", return_value=True),
            mock.patch.object(m.subprocess, "run", return_value=SimpleNamespace(returncode=1)),
            mock.patch.object(m, "read_windows_file_version") as fallback,
        ):
            with self.assertRaisesRegex(RuntimeError, "runner_version_probe_failed"):
                m.detect_runner_version(Path("runner"))
            fallback.assert_not_called()

    def test_standard_process_version_remains_primary(self) -> None:
        completed = SimpleNamespace(returncode=0, stdout="2.337.0", stderr="")
        with (
            mock.patch.object(m.Path, "is_file", return_value=True),
            mock.patch.object(m.subprocess, "run", return_value=completed),
            mock.patch.object(m, "read_windows_file_version") as fallback,
        ):
            self.assertEqual(m.detect_runner_version(Path("runner")), m.RunnerVersion(2, 337, 0))
            fallback.assert_not_called()

    def test_missing_metadata_does_not_claim_runner_supported(self) -> None:
        denied = OSError("application control")
        denied.winerror = 4551
        with (
            mock.patch.object(m.Path, "is_file", return_value=True),
            mock.patch.object(m.subprocess, "run", side_effect=denied),
            mock.patch.object(m, "read_windows_file_version", side_effect=RuntimeError("metadata missing")),
        ):
            with self.assertRaisesRegex(RuntimeError, "metadata missing"):
                m.detect_runner_version(Path("runner"))

    def test_wrong_binary_never_uses_version_metadata(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "runner_version_metadata_host_or_binary_invalid"):
            m.read_windows_file_version(Path("other.exe"))

    def test_metadata_does_not_bypass_minimum_version(self) -> None:
        fields = [0xFEEF04BD, 0x10000, (2 << 16) | 328, 0] + [0] * 9
        observed = m.version_from_fixed_file_info(fields)
        self.assertLess(observed, m.RunnerVersion(*m.REGISTRATION_MINIMUM))

    def test_repeat_metadata_parsing_is_idempotent(self) -> None:
        fields = [0xFEEF04BD, 0x10000, (2 << 16) | 337, 0] + [0] * 9
        self.assertEqual(m.version_from_fixed_file_info(fields), m.version_from_fixed_file_info(fields))


if __name__ == "__main__":
    unittest.main()

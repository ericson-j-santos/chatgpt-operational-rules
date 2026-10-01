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
            "remote_controller_semantic_ok": True,
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

    def test_local_task_with_online_but_semantically_unproven_controller_fails_closed(self) -> None:
        result = tr.evaluate({
            "task_type": "host_recovery",
            "capabilities": {"remote_desktop": True},
            "remote_controller_online": True,
            "remote_controller_semantic_ok": False,
            "remote_calls_left_pct": 16,
        })
        self.assertFalse(result["ready"])
        self.assertIsNone(result["selected_executor"])
        self.assertIn("remote_controller_semantic_unproven", result["reasons"])
        self.assertEqual(
            result["next_step"],
            "dual_host_preflight_or_controller_recovery",
        )

    def test_invalid_remote_percentage_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            tr.evaluate({
                "task_type": "local_machine",
                "capabilities": {"remote_desktop": True},
                "remote_controller_online": True,
                "remote_controller_semantic_ok": True,
                "remote_calls_left_pct": 101,
            })


class RemoteQuotaPolicyTests(unittest.TestCase):
    """Fixtures sintéticas; nenhum teste consulta dispositivos ou a conta RDC."""

    def local_payload(self, **changes):
        payload = {
            "task_type": "local_machine",
            "capabilities": {"remote_desktop": True},
            "remote_controller_online": True,
            "remote_controller_semantic_ok": True,
            "remote_calls_left_pct": 99,
            "correlation_id": "rdc-quota-policy-test",
        }
        payload.update(changes)
        return payload

    def renewed_payload(self, **changes):
        payload = self.local_payload(
            remote_quota_blocked=True,
            remote_quota_renewal_confirmed=True,
            quota_blocked_at="2025-01-30T12:00:00Z",
            quota_renewed_at="2025-02-01T00:00:00Z",
            quota_observed_at="2025-02-01T00:01:00Z",
            quota_renewal_source="official-account-test-fixture",
            reset_at="2025-02-01T00:00:00Z",
        )
        payload.update(changes)
        return payload

    def assert_blocked(self, result, reason):
        self.assertFalse(result["ready"])
        self.assertIsNone(result["selected_executor"])
        self.assertIn(reason, result["reasons"])
        self.assertEqual(result["next_step"], "use_non_rdc_executor_until_quota_verified")

    def test_zero_blocks_every_local_task_before_recovery_and_reserve(self):
        for task in sorted(tr.LOCAL_TYPES):
            with self.subTest(task=task):
                result = tr.evaluate(self.local_payload(task_type=task, remote_calls_left_pct=0))
                self.assert_blocked(result, "remote_quota_exhausted")
                self.assertTrue(result["remote_quota_blocked"])
                self.assertFalse(result["reserve_mode"])

    def test_unknown_quota_never_authorizes_local_task(self):
        self.assert_blocked(
            tr.evaluate(self.local_payload(remote_calls_left_pct=None)),
            "remote_quota_unknown",
        )

    def test_previous_exhaustion_requires_more_than_positive_balance(self):
        self.assert_blocked(
            tr.evaluate(self.local_payload(remote_quota_blocked=True)),
            "remote_quota_renewal_unproven",
        )

    def test_calendar_transition_alone_does_not_unlock(self):
        payload = self.renewed_payload(remote_quota_renewal_confirmed=False)
        self.assert_blocked(tr.evaluate(payload), "remote_quota_renewal_unproven")

    def test_confirmed_renewal_and_positive_balance_clear_previous_block(self):
        result = tr.evaluate(self.renewed_payload())
        self.assertTrue(result["ready"])
        self.assertEqual(result["selected_executor"], "remote_desktop")
        self.assertFalse(result["remote_quota_blocked"])
        self.assertIsNone(result["remote_quota_block_reason"])

    def test_unknown_reset_requires_independent_effective_renewal_evidence(self):
        for reset in (None, "unknown"):
            with self.subTest(reset=reset):
                result = tr.evaluate(self.renewed_payload(reset_at=reset))
                self.assertEqual(result["selected_executor"], "remote_desktop")
                self.assertFalse(result["remote_quota_blocked"])
                self.assert_blocked(
                    tr.evaluate(self.renewed_payload(reset_at=reset, quota_renewal_source="")),
                    "remote_quota_renewal_unproven",
                )

    def test_future_or_invalid_official_reset_blocks(self):
        for reset in ("2099-01-01T00:00:00Z", "invalid", "2025-02-01"):
            with self.subTest(reset=reset):
                self.assert_blocked(
                    tr.evaluate(self.renewed_payload(reset_at=reset)),
                    "remote_quota_reset_not_reached",
                )

    def test_incomplete_or_out_of_order_evidence_does_not_clear_block(self):
        cases = [
            {"quota_blocked_at": None},
            {"quota_renewed_at": None},
            {"quota_observed_at": None},
            {"quota_renewal_source": "   "},
            {"remote_quota_renewal_confirmed": "true"},
            {"quota_renewed_at": "2025-01-29T12:00:00Z"},
            {"quota_observed_at": "2025-01-31T12:00:00Z"},
            {"quota_observed_at": "2099-01-01T00:00:00Z"},
            {"quota_observed_at": "2025-02-01T00:01:00"},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                result = tr.evaluate(self.renewed_payload(**changes))
                self.assert_blocked(result, "remote_quota_renewal_unproven")
                self.assertTrue(result["remote_quota_blocked"])

    def test_renewal_does_not_override_zero_or_unknown_current_balance(self):
        for pct, reason in ((0, "remote_quota_exhausted"), (None, "remote_quota_unknown")):
            with self.subTest(pct=pct):
                result = tr.evaluate(self.renewed_payload(remote_calls_left_pct=pct))
                self.assert_blocked(result, reason)
                self.assertTrue(result["remote_quota_blocked"])

    def test_renewal_does_not_override_controller_semantic_checks(self):
        result = tr.evaluate(self.renewed_payload(remote_controller_semantic_ok=False))
        self.assertFalse(result["ready"])
        self.assertIsNone(result["selected_executor"])
        self.assertIn("remote_controller_semantic_unproven", result["reasons"])

    def test_reserve_boundaries_preserve_positive_local_budget_only(self):
        for pct in (0, 1, 20, 21, 100):
            with self.subTest(pct=pct):
                result = tr.evaluate(self.local_payload(remote_calls_left_pct=pct))
                self.assertEqual(result["reserve_mode"], 0 < pct <= 20)
                self.assertEqual(result["ready"], pct > 0)

    def test_native_connectors_are_not_blocked_by_rdc_exhaustion(self):
        for task, connector in tr.DIRECT_CONNECTORS.items():
            with self.subTest(task=task):
                payload = self.local_payload(
                    task_type=task,
                    capabilities={connector: True, "remote_desktop": True},
                    remote_calls_left_pct=0, remote_quota_blocked=True,
                )
                result = tr.evaluate(payload)
                self.assertEqual(result["selected_executor"], connector)
                self.assertTrue(result["remote_quota_blocked"])

    def test_preferred_executor_cannot_bypass_physical_task_requirement(self):
        for pct in (0, 99, None):
            with self.subTest(pct=pct):
                payload = self.local_payload(
                    task_type="custom", preferred_native_executor="remote_desktop",
                    remote_calls_left_pct=pct,
                )
                result = tr.evaluate(payload)
                self.assertFalse(result["ready"])
                self.assertIsNone(result["selected_executor"])
                self.assertIn("remote_requires_intrinsically_local_task", result["reasons"])

    def test_invalid_types_fail_closed(self):
        for value in (True, False, 1.5, float("nan"), float("inf"), {}, [], -1, 101):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    tr.evaluate(self.local_payload(remote_calls_left_pct=value))
        for value in ("false", 0, None):
            with self.subTest(blocked=value):
                with self.assertRaises(ValueError):
                    tr.evaluate(self.local_payload(remote_quota_blocked=value))
        for value in ([], None, "not-an-object"):
            with self.subTest(payload=value):
                with self.assertRaises(ValueError):
                    tr.evaluate(value)

    def cli(self, root, payload):
        """Executa o entrypoint real: JSON -> argumentos -> decisão -> arquivo -> releitura."""
        import contextlib
        import io
        import json
        import runpy
        from unittest.mock import patch

        input_path, output_path = root / "input.json", root / "decision.json"
        input_path.write_text(json.dumps(payload), encoding="utf-8")
        output = io.StringIO()
        argv = [str(SCRIPTS / "tool_router.py"), "--input", str(input_path), "--output", str(output_path)]
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as stopped:
                runpy.run_path(str(SCRIPTS / "tool_router.py"), run_name="__main__")
        readback = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(readback, json.loads(output.getvalue()))
        return stopped.exception.code, readback

    def test_cli_lifecycle_readback_replay_and_effective_renewal(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = self.local_payload(
                remote_calls_left_pct=0, quota_observed_at="2025-01-30T12:00:00Z",
                reset_at="2025-02-01T00:00:00Z",
            )
            code, exhausted = self.cli(root, payload)
            self.assertEqual(code, 3)
            self.assertTrue(exhausted["remote_quota_blocked"])
            self.assertEqual(exhausted["quota_blocked_at"], payload["quota_observed_at"])
            self.assertEqual((code, exhausted), self.cli(root, payload))
            carried = {key: exhausted[key] for key in ("remote_quota_blocked", "quota_blocked_at", "reset_at")}
            code, still_blocked = self.cli(root, self.local_payload(**carried))
            self.assertEqual(code, 3)
            self.assertTrue(still_blocked["remote_quota_blocked"])
            code, renewed = self.cli(root, self.renewed_payload(**carried))
            self.assertEqual(code, 0)
            self.assertEqual(renewed["selected_executor"], "remote_desktop")
            self.assertFalse(renewed["remote_quota_blocked"])
            self.assertEqual(renewed["correlation_id"], "rdc-quota-policy-test")

    def test_cli_invalid_input_replaces_old_positive_decision(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, positive = self.cli(root, self.local_payload())
            self.assertEqual(code, 0)
            self.assertTrue(positive["ready"])
            code, failed = self.cli(root, ["invalid"])
            self.assertEqual(code, 2)
            self.assertFalse(failed["ready"])
            self.assertIsNone(failed["selected_executor"])
            self.assertIn("error", failed)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import dual_host_preflight as dhp


SHA = "a" * 40


def host(name: str, **overrides):
    value = {
        "name": name,
        "controller_online": True,
        "auth_valid": True,
        "rules_sha": SHA,
        "session_launch_ok": True,
        "state_validated": True,
        "gateway_ok": True,
        "controller_semantic_ok": True,
        "status_count": 0,
        "controller_version": "0.2.51",
        "active_tasks": 0,
        "capacity_score": 50,
        "reserved_worktrees": [],
    }
    value.update(overrides)
    return value


class DualHostPreflightTests(unittest.TestCase):
    def test_preferred_host_wins_when_both_are_healthy(self) -> None:
        result = dhp.evaluate({
            "required_rules_sha": SHA,
            "preferred_host": "DESKTOP-PDQK954",
            "hosts": [host("Noteri"), host("DESKTOP-PDQK954")],
        })
        self.assertTrue(result["ready"])
        self.assertEqual(result["selected_host"], "DESKTOP-PDQK954")
        self.assertEqual(result["secondary_host"], "Noteri")
        desktop = next(
            item for item in result["evaluated_hosts"]
            if item["name"] == "DESKTOP-PDQK954"
        )
        self.assertTrue(desktop["host_reachable"])
        self.assertEqual(desktop["availability_state"], "controller_online")

    def test_controller_offline_host_unknown_is_blocked(self) -> None:
        result = dhp.evaluate({
            "required_rules_sha": SHA,
            "hosts": [
                host("Noteri"),
                host("DESKTOP-PDQK954", controller_online=False),
            ],
        })
        self.assertEqual(result["selected_host"], "Noteri")
        self.assertIn("controller_online", result["blocked_hosts"]["DESKTOP-PDQK954"])
        desktop = next(
            item for item in result["evaluated_hosts"]
            if item["name"] == "DESKTOP-PDQK954"
        )
        self.assertIsNone(desktop["host_reachable"])
        self.assertEqual(
            desktop["availability_state"], "controller_offline_host_unknown"
        )
        self.assertEqual(desktop["recovery_action"], "probe_host_independently")

    def test_reachable_host_with_controller_offline_is_not_called_offline(self) -> None:
        result = dhp.evaluate({
            "required_rules_sha": SHA,
            "hosts": [
                host("Noteri"),
                host(
                    "DESKTOP-PDQK954",
                    controller_online=False,
                    host_reachable=True,
                ),
            ],
        })
        self.assertEqual(result["selected_host"], "Noteri")
        self.assertIn("controller_online", result["blocked_hosts"]["DESKTOP-PDQK954"])
        desktop = next(
            item for item in result["evaluated_hosts"]
            if item["name"] == "DESKTOP-PDQK954"
        )
        self.assertTrue(desktop["host_reachable"])
        self.assertFalse(desktop["eligible"])
        self.assertEqual(
            desktop["availability_state"], "host_reachable_controller_offline"
        )
        self.assertEqual(desktop["recovery_action"], "restart_controller")

    def test_independent_signal_proves_host_reachable_without_controller(self) -> None:
        result = dhp.evaluate({
            "required_rules_sha": SHA,
            "hosts": [
                host("Noteri"),
                host(
                    "DESKTOP-PDQK954",
                    controller_online=False,
                    reachability_signals={
                        "windows_rpc": True,
                        "rdc_heartbeat": False,
                    },
                ),
            ],
        })
        desktop = next(
            item for item in result["evaluated_hosts"]
            if item["name"] == "DESKTOP-PDQK954"
        )
        self.assertTrue(desktop["host_reachable"])
        self.assertEqual(desktop["reachability_signals"], ["windows_rpc"])
        self.assertEqual(
            desktop["availability_state"], "host_reachable_controller_offline"
        )

    def test_explicit_host_unreachable_stays_distinct_from_controller_state(self) -> None:
        result = dhp.evaluate({
            "required_rules_sha": SHA,
            "hosts": [
                host("Noteri"),
                host(
                    "DESKTOP-PDQK954",
                    controller_online=False,
                    host_reachable=False,
                ),
            ],
        })
        desktop = next(
            item for item in result["evaluated_hosts"]
            if item["name"] == "DESKTOP-PDQK954"
        )
        self.assertFalse(desktop["host_reachable"])
        self.assertEqual(desktop["availability_state"], "host_unreachable")
        self.assertEqual(desktop["recovery_action"], "restore_host_or_network")

    def test_sha_mismatch_fails_closed(self) -> None:
        result = dhp.evaluate({
            "required_rules_sha": SHA,
            "hosts": [
                host("Noteri", rules_sha="b" * 40),
                host("DESKTOP-PDQK954"),
            ],
        })
        self.assertEqual(result["selected_host"], "DESKTOP-PDQK954")
        self.assertIn("rules_sha_mismatch", result["blocked_hosts"]["Noteri"])

    def test_reserved_worktree_is_not_reused(self) -> None:
        worktree = r"C:\dev\chatgpt-workers\wt-chat-task-123"
        result = dhp.evaluate({
            "required_rules_sha": SHA,
            "requested_worktree": worktree,
            "hosts": [
                host("Noteri", reserved_worktrees=[worktree]),
                host("DESKTOP-PDQK954"),
            ],
        })
        self.assertEqual(result["selected_host"], "DESKTOP-PDQK954")
        self.assertIn("worktree_reserved", result["blocked_hosts"]["Noteri"])

    def test_controller_version_mismatch_is_warning_only(self) -> None:
        result = dhp.evaluate({
            "required_rules_sha": SHA,
            "hosts": [
                host("Noteri", controller_version="0.2.50"),
                host("DESKTOP-PDQK954", controller_version="0.2.51"),
            ],
        })
        self.assertTrue(result["ready"])
        self.assertIn("controller_version_mismatch", result["warnings"])

    def test_load_reduces_route_score(self) -> None:
        result = dhp.evaluate({
            "required_rules_sha": SHA,
            "hosts": [
                host("Noteri", active_tasks=0, capacity_score=50),
                host("DESKTOP-PDQK954", active_tasks=2, capacity_score=60),
            ],
        })
        self.assertEqual(result["selected_host"], "Noteri")

    def test_no_eligible_hosts_returns_not_ready(self) -> None:
        result = dhp.evaluate({
            "required_rules_sha": SHA,
            "hosts": [
                host("Noteri", gateway_ok=False),
                host("DESKTOP-PDQK954", auth_valid=False),
            ],
        })
        self.assertFalse(result["ready"])
        self.assertIsNone(result["selected_host"])

    def test_estudo_profile_routes_development_to_other_host(self) -> None:
        result = dhp.evaluate({
            "required_rules_sha": SHA,
            "requested_workload": "development",
            "preferred_host": "Noteri",
            "hosts": [
                host("Noteri", profile="ESTUDO", capacity_score=100),
                host("DESKTOP-PDQK954", profile="NORMAL", capacity_score=50),
            ],
        })
        self.assertTrue(result["ready"])
        self.assertEqual(result["selected_host"], "DESKTOP-PDQK954")
        self.assertIn("profile_estudo", result["blocked_hosts"]["Noteri"])
        self.assertFalse(next(x for x in result["evaluated_hosts"] if x["name"] == "Noteri")["accepts_new_development"])

    def test_estudo_profile_keeps_monitoring_eligible(self) -> None:
        result = dhp.evaluate({
            "required_rules_sha": SHA,
            "requested_workload": "monitoring",
            "preferred_host": "Noteri",
            "hosts": [
                host("Noteri", profile="ESTUDO"),
                host("DESKTOP-PDQK954", profile="NORMAL"),
            ],
        })
        self.assertTrue(result["ready"])
        self.assertEqual(result["selected_host"], "Noteri")
        self.assertNotIn("Noteri", result["blocked_hosts"])

    def test_invalid_profile_fails_closed(self) -> None:
        result = dhp.evaluate({
            "required_rules_sha": SHA,
            "hosts": [
                host("Noteri", profile="UNKNOWN"),
                host("DESKTOP-PDQK954"),
            ],
        })
        self.assertEqual(result["selected_host"], "DESKTOP-PDQK954")
        self.assertIn("invalid_profile", result["blocked_hosts"]["Noteri"])

    def test_invalid_sha_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            dhp.evaluate({
                "required_rules_sha": "abc123",
                "hosts": [host("Noteri"), host("DESKTOP-PDQK954")],
            })


    def test_semantic_controller_failure_blocks_host(self) -> None:
        payload = self.payload()
        payload["hosts"][0]["controller_semantic_ok"] = False
        result = dhp.evaluate(payload)
        blocked = result["blocked_hosts"].get(payload["hosts"][0]["name"], [])
        self.assertIn("controller_semantic_ok", blocked)



if __name__ == "__main__":
    unittest.main()

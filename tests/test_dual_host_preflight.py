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
        payload = {
            "required_rules_sha": SHA,
            "hosts": [
                host("Noteri", controller_semantic_ok=False),
                host("DESKTOP-PDQK954"),
            ],
        }
        result = dhp.evaluate(payload)
        self.assertEqual(result["selected_host"], "DESKTOP-PDQK954")
        self.assertIn("controller_semantic_ok", result["blocked_hosts"]["Noteri"])




# Synthetic API/receipt fixtures test the selector, not a physical SQL Server.
import copy
import contextlib
import io
import json
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch


class TransportAwarePreflightTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        stamp = self.now.isoformat()
        self.correlation = "noteri-hml-transport-control"
        repo = "ericson-j-santos/noteri-runtime"
        self.job = {
            "id": 11, "run_id": 22, "runner_id": 33, "run_attempt": 1,
            "runner_name": "Noteri", "head_sha": "b" * 40,
            "status": "completed", "conclusion": "success",
            "completed_at": stamp, "labels": ["self-hosted", "Windows", "X64"],
            "html_url": f"https://github.com/{repo}/actions/runs/22/job/11",
            "steps": [{"name": dhp.QUALIFICATION_STEP, "status": "completed", "conclusion": "success"}],
        }
        self.receipt = {
            "host": "Noteri", "repository": repo,
            "source_sha": "b" * 40, "rules_sha": SHA, "run_id": 22,
            "job_id": 11, "runner_id": 33, "run_attempt": 1,
            "runner_name": "Noteri", "correlation_id": self.correlation,
            "profile": "NORMAL", "generated_at": stamp, "session_id": "noteri-test-session",
            "session_result": "SESSION_LAUNCH_OK", "snapshot_sha256": "c" * 64,
            "state_validated": True, "gateway_ok": True, "auth_valid": True,
            "independent_effect_verified": True,
            "capabilities": {"sqlserver.major": "17", "sqlserver.runtime": "native"},
        }
        self.proof = {
            "schema_version": "1", "source": "github_api_and_gateway_receipt",
            "repository": repo, "observed_at": stamp,
            "independent_readback_verified": True,
            "artifact_sha256": "d" * 64, "readback_sha256": "e" * 64,
            "job": self.job, "receipt": self.receipt,
        }
        self.noteri = host(
            "Noteri", controller_online=False, controller_semantic_ok=False,
            transport="github_actions", transport_evidence=self.proof,
            execution_repository=repo, expected_source_sha="b" * 40,
            runner_name="Noteri", profile="NORMAL",
        )
        self.payload = {
            "required_rules_sha": SHA, "correlation_id": self.correlation,
            "requested_workload": "hml", "preferred_host": "DESKTOP-PDQK954",
            "required_capabilities": {"sqlserver.major": "17", "sqlserver.runtime": "native"},
            "hosts": [self.noteri, host("DESKTOP-PDQK954", controller_online=False)],
        }

    def assess(self):
        return dhp.evaluate(self.payload)

    def blocked(self, reason):
        result = self.assess()
        self.assertFalse(result["ready"])
        self.assertIn(reason, result["blocked_hosts"]["Noteri"])
        self.assertFalse(result["hml_verified"])
        return result

    def test_actions_works_without_rdc_and_no_recovery(self):
        result = self.assess()
        self.assertTrue(result["ready"])
        self.assertEqual("Noteri", result["selected_host"])
        self.assertFalse(result["hml_verified"])
        self.assertTrue(result["decision_only"])
        item = result["evaluated_hosts"][0]
        self.assertFalse(item["controller_online"])
        self.assertTrue(item["transport_valid"])
        self.assertFalse(item["recovery_required"])
        self.assertEqual("github_actions_verified", item["availability_state"])

    def test_missing_proof_blocks(self):
        self.noteri.pop("transport_evidence")
        self.blocked("github_actions_evidence_missing")

    def test_no_runner_assignment_blocks_even_when_success_claimed(self):
        self.job["runner_id"] = 0
        self.blocked("github_actions_no_pickup")

    def test_boolean_runner_id_does_not_count(self):
        self.job["runner_id"] = True
        self.blocked("github_actions_no_pickup")

    def test_cancelled_blocks(self):
        self.job["conclusion"] = "cancelled"
        self.blocked("github_actions_job_not_successful")

    def test_queued_does_not_count(self):
        self.job.update(status="queued", conclusion=None)
        self.blocked("github_actions_job_not_successful")

    def test_empty_steps_blocks(self):
        self.job["steps"] = []
        self.blocked("github_actions_qualification_not_executed")

    def test_skipped_qualification_blocks(self):
        self.job["steps"][0]["conclusion"] = "skipped"
        self.blocked("github_actions_qualification_not_executed")

    def test_setup_job_is_not_qualification(self):
        self.job["steps"][0]["name"] = "Set up job"
        self.blocked("github_actions_qualification_not_executed")

    def test_evidence_expired_blocks(self):
        self.proof["observed_at"] = (self.now - timedelta(seconds=301)).isoformat()
        self.blocked("github_actions_evidence_not_fresh")

    def test_fresh_api_does_not_renew_old_job(self):
        self.job["completed_at"] = (self.now - timedelta(days=1)).isoformat()
        self.blocked("github_actions_evidence_not_fresh")

    def test_future_observation_blocks(self):
        self.proof["observed_at"] = (self.now + timedelta(minutes=5)).isoformat()
        self.blocked("github_actions_evidence_not_fresh")

    def test_missing_timezone_blocks(self):
        self.proof["observed_at"] = self.now.replace(tzinfo=None).isoformat()
        self.blocked("github_actions_evidence_not_fresh")

    def test_unverified_readback_blocks(self):
        self.proof["independent_readback_verified"] = "true"
        self.blocked("github_actions_readback_unverified")

    def test_missing_artifact_integrity_blocks(self):
        self.proof.pop("artifact_sha256")
        self.blocked("github_actions_integrity_missing")

    def test_wrong_runner_blocks(self):
        self.job["runner_name"] = "SomeOtherPC"
        self.blocked("github_actions_target_mismatch")

    def test_wrong_repository_url_blocks(self):
        self.job["html_url"] = "https://example.invalid/run"
        self.blocked("github_actions_target_mismatch")

    def test_old_source_sha_blocks(self):
        self.job["head_sha"] = "f" * 40
        self.blocked("github_actions_target_mismatch")

    def test_cloud_runner_cannot_impersonate_noteri(self):
        self.job["labels"] = ["ubuntu-latest"]
        self.blocked("github_actions_not_self_hosted")

    def test_missing_expected_identity_blocks(self):
        self.noteri.pop("runner_name")
        self.blocked("github_actions_expected_target_missing")

    def test_wrong_receipt_host_blocks(self):
        self.receipt["host"] = "DESKTOP-PDQK954"
        self.blocked("github_actions_receipt_binding_mismatch")

    def test_wrong_receipt_rules_sha_blocks(self):
        self.receipt["rules_sha"] = "f" * 40
        self.blocked("github_actions_receipt_binding_mismatch")

    def test_different_run_attempt_blocks(self):
        self.receipt["run_attempt"] = 2
        self.blocked("github_actions_receipt_binding_mismatch")

    def test_different_correlation_blocks(self):
        self.receipt["correlation_id"] = "previous-request"
        self.blocked("github_actions_receipt_binding_mismatch")

    def test_old_receipt_blocks(self):
        self.receipt["generated_at"] = (self.now-timedelta(seconds=301)).isoformat()
        self.blocked("github_actions_receipt_not_fresh")

    def test_session_launch_failure_blocks(self):
        self.receipt["session_result"] = "SESSION_LAUNCH_BLOCKED"
        self.blocked("github_actions_session_not_launched")

    def test_gateway_false_in_receipt_blocks(self):
        self.receipt["gateway_ok"] = False
        self.blocked("github_actions_session_unverified")

    def test_missing_snapshot_blocks(self):
        self.receipt.pop("snapshot_sha256")
        self.blocked("github_actions_session_snapshot_missing")

    def test_global_gateway_gate_preserved(self):
        self.noteri["gateway_ok"] = False
        self.blocked("gateway_ok")

    def test_global_auth_gate_preserved(self):
        self.noteri["auth_valid"] = False
        self.blocked("auth_valid")

    def test_dirty_worktree_blocks(self):
        self.noteri["status_count"] = 1
        self.blocked("worktree_dirty")

    def test_boolean_worktree_count_blocks(self):
        self.noteri["status_count"] = False
        self.blocked("worktree_state_unverified")

    def test_reserved_worktree_blocks(self):
        self.payload["requested_worktree"] = r"C:\dev\chatgpt-workers\wt-one"
        self.noteri["reserved_worktrees"] = [self.payload["requested_worktree"]]
        self.blocked("worktree_reserved")

    def test_reachability_conflict_blocks(self):
        self.noteri["host_reachable"] = False
        self.blocked("github_actions_reachability_conflict")

    def test_missing_capability_cannot_be_supplied_by_label(self):
        self.receipt["capabilities"].pop("sqlserver.major")
        self.job["labels"].append("sqlserver-17")
        self.noteri["capabilities"] = {"sqlserver.major": "17"}
        self.blocked("capability_missing:sqlserver.major")

    def test_wrong_version_blocks(self):
        self.receipt["capabilities"]["sqlserver.major"] = "16"
        self.blocked("capability_missing:sqlserver.major")

    def test_estudo_blocks_hml(self):
        self.noteri["profile"] = self.receipt["profile"] = "ESTUDO"
        self.blocked("profile_estudo")

    def test_profile_must_be_observed_for_actions(self):
        self.noteri.pop("profile")
        self.receipt.pop("profile")
        self.blocked("github_actions_profile_unverified")

    def test_unknown_transport_never_falls_back_to_rdc(self):
        self.noteri["transport"] = "ssh"
        self.noteri["controller_online"] = self.noteri["controller_semantic_ok"] = True
        self.blocked("transport_not_allowed")

    def test_malformed_transport_fails_closed(self):
        self.noteri["transport"] = {"kind": "rdc"}
        self.blocked("transport_not_allowed")

    def test_invalid_capability_schema_rejected(self):
        self.payload["required_capabilities"] = {"sqlserver.major": True}
        with self.assertRaises(ValueError):
            self.assess()

    def test_non_hex_sha_rejected(self):
        self.payload["required_rules_sha"] = "z" * 40
        with self.assertRaises(ValueError):
            self.assess()

    def test_duplicate_case_insensitive_host_names_rejected(self):
        self.payload["hosts"][1]["name"] = "noteri"
        with self.assertRaises(ValueError):
            self.assess()

    def test_repeat_is_deterministic_and_does_not_mutate(self):
        before = copy.deepcopy(self.payload)
        self.assertEqual(self.assess(), self.assess())
        self.assertEqual(before, self.payload)

    def test_no_raw_receipts_in_result(self):
        self.proof["private_canary"] = "never-export-marker"
        self.assertNotIn("never-export-marker", json.dumps(self.assess()))

    def test_cli_roundtrip_and_negative_control(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.json"
            target = Path(directory) / "result.json"
            source.write_text(json.dumps(self.payload), encoding="utf-8")
            argv = ["dual_host_preflight", "--input", str(source), "--output", str(target)]
            with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, dhp.main())
            self.assertEqual("Noteri", json.loads(target.read_text())["selected_host"])
            self.job["runner_id"] = 0
            source.write_text(json.dumps(self.payload), encoding="utf-8")
            with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(3, dhp.main())
            self.assertIsNone(json.loads(target.read_text())["selected_host"])



    def test_cli_invalid_input_replaces_stale_success(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.json"
            target = Path(directory) / "result.json"
            source.write_text("not-json", encoding="utf-8")
            target.write_text('{"ready":true,"selected_host":"Noteri"}', encoding="utf-8")
            argv = ["dual_host_preflight", "--input", str(source), "--output", str(target)]
            with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(2, dhp.main())
            self.assertFalse(json.loads(target.read_text())["ready"])
            self.assertEqual([], list(Path(directory).glob("*.tmp")))

    def test_cli_output_failure_never_prints_ready_true(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.json"
            target = Path(directory) / "absent" / "result.json"
            source.write_text(json.dumps(self.payload), encoding="utf-8")
            argv = ["dual_host_preflight", "--input", str(source), "--output", str(target)]
            stream = io.StringIO()
            with patch.object(sys, "argv", argv), contextlib.redirect_stdout(stream):
                self.assertEqual(2, dhp.main())
            self.assertFalse(json.loads(stream.getvalue())["ready"])

    def test_actions_all_common_gates_preserved(self):
        for field in ("auth_valid", "session_launch_ok", "state_validated", "gateway_ok"):
            with self.subTest(field=field):
                previous = self.noteri[field]
                self.noteri[field] = False
                self.blocked(field)
                self.noteri[field] = previous

    def test_two_ready_hosts_select_by_capacity_without_duplicate_work(self):
        desktop = host(
            "DESKTOP-PDQK954", profile="NORMAL", capacity_score=10,
            capabilities={"sqlserver.major":"17", "sqlserver.runtime":"native"},
        )
        self.payload["hosts"][1] = desktop
        result = self.assess()
        self.assertEqual("Noteri", result["selected_host"])
        self.assertEqual("DESKTOP-PDQK954", result["secondary_host"])
        self.assertEqual(2,len(result["eligible_hosts"]))



    def test_invalid_repository_scope_blocks(self):
        self.noteri["execution_repository"] = "../.."
        self.blocked("github_actions_expected_target_missing")

    def test_atomic_failure_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "result.json"
            with patch.object(dhp.os, "replace", side_effect=OSError("fixture")):
                with self.assertRaises(OSError):
                    dhp._write_evidence(target, "{}")
            self.assertEqual([],list(Path(directory).iterdir()))


    def test_cli_malformed_host_replaces_stale_success(self):
        for field, value in (("reserved_worktrees", 7), ("active_tasks", float("inf"))):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp:
                source, target = Path(temp) / "input.json", Path(temp) / "output.json"
                payload = copy.deepcopy(self.payload)
                payload["hosts"][0][field] = value
                source.write_text(json.dumps(payload), encoding="utf-8")
                target.write_text('{"ready":true}', encoding="utf-8")
                with patch.object(sys, "argv", ["preflight", "--input", str(source), "--output", str(target)]):
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(2, dhp.main())
                self.assertFalse(json.loads(target.read_text(encoding="utf-8"))["ready"])

    def test_malformed_profile_cannot_bypass_validation(self):
        self.noteri["profile"] = []
        self.receipt["profile"] = []
        self.blocked("github_actions_profile_unverified")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import copy
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import reqsys_dual_desktop_guard as gate

SHA = "a" * 40
NOW = datetime(2026, 11, 12, 21, 30, tzinfo=timezone.utc)


def example():
    names = gate.ROLES
    hosts = []
    assignments = []
    for role, name in names.items():
        ordinal = 1 if role == "builder" else 2
        proof = dict(
            source="github_api_and_gateway_receipt",
            independent_readback_verified=True, artifact_bytes_verified=True,
            observed_at=NOW.isoformat(), job_completed_at=NOW.isoformat(),
            artifact_sha256=str(ordinal) * 64,
            readback_sha256=str(ordinal + 2) * 64,
            physical_identity_sha256=str(ordinal + 4) * 64,
            runner_id=ordinal, job_id=ordinal, run_id=ordinal, run_attempt=1,
            runner_name=name + "-runtime", host=name, role=role, rules_sha=SHA,
            correlation_id="reqsys-dual-host-test-20261112", source_sha=SHA,
            job_head_sha=SHA, qualification_step="completed/success",
            job_status="completed", job_conclusion="success",
            isolated_workspace_verified=True, validation_read_only=(role == "validator"),
        )
        hosts.append(dict(name=name, role=role, transport="github_actions",
            profile="NORMAL", rules_sha=SHA, status_count=0,
            auth_valid=True, session_launch_ok=True, state_validated=True,
            gateway_ok=True, runner_name=name + "-runtime", physical_proof=proof))
        assignments.append(dict(role=role, host=name, task_id="task-" + role,
            branch="dev/" + role, worktree="C:/workers/" + role,
            lease_token_digest=str(ordinal + 6) * 64, lease_generation=ordinal,
            exclusive_lease_verified=True, read_only=(role == "validator"),
            task_source_sha=SHA))
    return dict(required_rules_sha=SHA, correlation_id="reqsys-dual-host-test-20261112",
        environment="DEV", cost_state="COST_ZERO_CONFIRMED",
        independent_cost_verification=True, hosts=hosts, assignments=assignments)


class GuardTests(unittest.TestCase):
    def result(self, payload):
        return gate.evaluate(payload, now=NOW)

    def blocked(self, mutate, reason):
        data = example()
        mutate(data)
        output = self.result(data)
        self.assertFalse(output["pair_candidate_consistent"])
        self.assertIn(reason, output["blockers"])
        self.assertFalse(output["ready_for_work"])
        self.assertFalse(output["dispatch_authorized"])

    def test_valid_synthetic_pair_is_only_candidate(self):
        a = example()
        r = self.result(a)
        self.assertTrue(r["pair_candidate_consistent"])
        self.assertFalse(r["ready_for_work"])
        self.assertFalse(r["physical_simultaneity_verified"])
        self.assertFalse(r["cross_host_replay_verified"])
        self.assertFalse(r["dispatch_authorized"])
        self.assertEqual(r, self.result(copy.deepcopy(a)))

    def test_missing_second_host(self):
        self.blocked(lambda d: d["hosts"].pop(), "exactly_two_host_records_required")

    def test_other_machine_cannot_replace_validator(self):
        self.blocked(lambda d: d["hosts"][1].update(name="Noteri"), "physical_pair_mismatch")

    def test_swapped_role(self):
        self.blocked(lambda d: d["hosts"][1].update(role="builder"), "validator:role_mismatch")

    def test_same_physical_identity(self):
        self.blocked(lambda d: d["hosts"][1]["physical_proof"].update(
            physical_identity_sha256=d["hosts"][0]["physical_proof"]["physical_identity_sha256"]),
            "same_physical_machine")

    def test_same_runner(self):
        self.blocked(lambda d: d["hosts"][1]["physical_proof"].update(runner_id=1), "same_runner")

    def test_missing_physical_receipt(self):
        self.blocked(lambda d: d["hosts"][1].pop("physical_proof"), "validator:physical_receipt_missing")

    def test_old_job_does_not_become_fresh_on_new_observation(self):
        self.blocked(lambda d: d["hosts"][1]["physical_proof"].update(
            job_completed_at=(NOW - timedelta(hours=1)).isoformat()), "validator:physical_proof_stale")

    def test_false_readback_is_not_enough(self):
        self.blocked(lambda d: d["hosts"][1]["physical_proof"].update(
            independent_readback_verified=False), "validator:independent_proof_missing")

    def test_unchecked_artifact_bytes_are_blocked(self):
        self.blocked(lambda d: d["hosts"][1]["physical_proof"].update(
            artifact_bytes_verified=False), "validator:independent_proof_missing")

    def test_validator_cannot_write(self):
        self.blocked(lambda d: d["assignments"][1].update(read_only=False),
                     "validator:assignment_not_read_only")

    def test_validator_scope_must_be_attested(self):
        self.blocked(lambda d: d["hosts"][1]["physical_proof"].update(
            validation_read_only=False), "validator:write_scope_not_blocked")

    def test_same_task_cannot_be_owned_twice(self):
        self.blocked(lambda d: d["assignments"][1].update(task_id="task-builder"),
                     "duplicate_task_ownership")

    def test_same_branch_cannot_be_written_by_two_lanes(self):
        self.blocked(lambda d: d["assignments"][1].update(branch="dev/builder"),
                     "duplicate_branch_ownership")

    def test_same_worktree_is_blocked_conservatively(self):
        self.blocked(lambda d: d["assignments"][1].update(worktree="C:/workers/builder"),
                     "duplicate_worktree_ownership")

    def test_same_lease_is_blocked(self):
        self.blocked(lambda d: d["assignments"][1].update(lease_token_digest="7" * 64),
                     "duplicate_lease_ownership")

    def test_missing_exclusive_lease(self):
        self.blocked(lambda d: d["assignments"][1].update(exclusive_lease_verified=False),
                     "validator:exclusive_lease_unverified")

    def test_unknown_cost(self):
        self.blocked(lambda d: d.update(cost_state="COST_UNKNOWN_BLOCKED"), "cost_unverified")

    def test_stale_rules_sha(self):
        self.blocked(lambda d: d["hosts"][0].update(rules_sha="f" * 40),
                     "builder:rules_sha_mismatch")

    def test_wrong_environment(self):
        self.blocked(lambda d: d.update(environment="PROD"), "environment_not_dev")

    def test_legacy_rdc_is_not_physical_job_proof(self):
        self.blocked(lambda d: d["hosts"][0].update(transport="rdc"),
                     "builder:physical_transport_not_proven")

    def test_skipped_physical_qualification(self):
        self.blocked(lambda d: d["hosts"][1]["physical_proof"].update(
            qualification_step="skipped"), "validator:physical_qualification_not_passed")

    def test_fake_bool_runner_id_is_rejected(self):
        self.blocked(lambda d: d["hosts"][1]["physical_proof"].update(runner_id=True),
                     "validator:runner_id_invalid")

    def test_duplicate_assignment_role(self):
        self.blocked(lambda d: d["assignments"][1].update(role="builder"),
                     "validator:assignment_missing_or_duplicate")


if __name__ == "__main__":
    unittest.main()

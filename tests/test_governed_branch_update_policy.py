"""Regression tests for preserving both gateway capabilities after PR #147."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import command_gateway as cg


class GovernedBranchPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = cg.load_policy(
            ROOT / "config" / "command-gateway.policy.json",
            enforce_platform=False,
            validate_paths=False,
        )
        self.record = dict(self.policy["governed_branch_updates"][0])
        self.record["script"] = str(ROOT / "scripts" / "update_branch_fast_forward_governed.py")
        self.policy["governed_branch_updates"] = [self.record]
        self.cwd = Path(self.record["repo_root"])
        self.command = [
            "python", self.record["script"],
            "--repo", self.record["repo_root"],
            "--remote", self.record["remote"],
            "--branch", self.record["branch"],
            "--expected-remote", self.record["expected_remote"],
            "--expected-head", self.record["expected_head"],
            "--correlation-id", "pr147-regression",
        ]

    def test_reviewed_invocation_is_repeatable_and_does_not_mutate_policy(self) -> None:
        before = copy.deepcopy(self.policy)
        for _ in range(2):
            cg.validate_command(self.command, 2, self.policy, self.cwd)
        self.assertEqual(before, self.policy)

    def test_multiple_records_select_exactly_one_and_reject_ambiguity(self) -> None:
        second = dict(self.record)
        second.update({
            "repo_root": r"C:\dev\chatgpt-workers\wt-chat-engineering-worker-pool-base-20261022-a1",
            "repository": "ericson-j-santos/engineering-worker-pool",
            "branch": "codex/windows-native-no-docker-20261022",
            "expected_remote": "8740def7e832916452b13c52a1f4bec2f5712df7",
            "expected_head": "cd150974a094dcda240987d8a2283c709a714d36",
        })
        policy = copy.deepcopy(self.policy)
        policy["governed_branch_updates"] = [self.record, second]
        command = [
            "python", second["script"],
            "--repo", second["repo_root"],
            "--remote", second["remote"],
            "--branch", second["branch"],
            "--expected-remote", second["expected_remote"],
            "--expected-head", second["expected_head"],
            "--correlation-id", "worker-update-regression",
        ]
        cg.validate_command(command, 2, policy, Path(second["repo_root"]))

        policy["governed_branch_updates"].append(dict(second))
        with self.assertRaisesRegex(cg.GatewayError, "ambiguous"):
            cg.validate_command(command, 2, policy, Path(second["repo_root"]))

    def test_branch_force_and_incomplete_invocations_are_blocked(self) -> None:
        changed_branch = list(self.command)
        changed_branch[changed_branch.index("--branch") + 1] = "main"
        for command in (
            changed_branch, self.command + ["--force"],
            self.command + ["--force-with-lease"], self.command[:-2],
        ):
            with self.subTest(command=command):
                with self.assertRaises(cg.GatewayError):
                    cg.validate_command(command, 2, self.policy, self.cwd)

    def test_missing_capability_and_different_worktree_are_blocked(self) -> None:
        without_capability = copy.deepcopy(self.policy)
        without_capability.pop("governed_branch_updates")
        with self.assertRaises(cg.GatewayError):
            cg.validate_command(self.command, 2, without_capability, self.cwd)
        with self.assertRaises(cg.GatewayError):
            cg.validate_command(self.command, 2, self.policy, self.cwd / "different")

    @unittest.skipIf(os.name == "nt", "retarget de symlink validado em POSIX")
    def test_governed_script_cannot_be_retargeted_after_policy_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            script = root / "update_branch_fast_forward_governed.py"
            script.write_text("# reviewed helper\n", encoding="utf-8")
            record = dict(self.record)
            record["script"] = str(script)
            record["repo_root"] = str(repo)
            policy = copy.deepcopy(self.policy)
            policy["governed_branch_updates"] = [record]
            command = list(self.command)
            command[1] = str(script)
            command[command.index("--repo") + 1] = str(repo)
            cg.validate_governed_branch_update(command, repo, policy)

            reviewed = root / "reviewed-helper.py"
            script.rename(reviewed)
            malicious = root / "malicious-helper.py"
            malicious.write_text("# substituted helper\n", encoding="utf-8")
            script.symlink_to(malicious)
            with self.assertRaisesRegex(cg.GatewayError, "symlink ou junction"):
                cg.validate_governed_branch_update(command, repo, policy)

    def test_direct_push_and_risk3_remain_blocked(self) -> None:
        with self.assertRaises(cg.GatewayError):
            cg.validate_command(["git", "push"], 2, self.policy, self.cwd)
        with self.assertRaises(cg.GatewayError):
            cg.validate_command(self.command, 3, self.policy, self.cwd)

    def test_policy_preserves_executable_specs_and_session_guards(self) -> None:
        self.assertIn("allowed_executable_specs", self.policy)
        self.assertEqual([], self.policy["allowed_executable_specs"])
        for key in (
            "require_session_bootstrap", "require_preflight_snapshot",
            "risk2_requires_clean_tree", "deny_inline_code",
        ):
            self.assertIs(self.policy[key], True)

    def test_pinned_executable_hash_is_still_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "synthetic-check"
            executable.write_bytes(b"synthetic fixture, never executed")
            policy = copy.deepcopy(self.policy)
            policy["allowed_executable_specs"] = [{
                "path": str(executable),
                "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
            }]
            cg.validate_command([str(executable)], 1, policy, self.cwd)
            executable.write_bytes(b"changed synthetic fixture")
            with self.assertRaisesRegex(cg.GatewayError, "integridade"):
                cg.validate_command([str(executable)], 1, policy, self.cwd)

    def assert_ci_contract(self, workflow: str) -> None:
        self.assertIn("image: postgres:16", workflow)
        self.assertNotIn("public.ecr.aws/docker/library/postgres", workflow)
        self.assertIn(
            "run: python -m unittest -v tests/test_command_gateway.py "
            "tests/test_command_gateway_posix_policy.py "
            "tests/test_validate_rules_policy_profiles.py "
            "tests/test_governed_branch_update_policy.py "
            "tests/test_update_branch_fast_forward_governed.py",
            workflow,
        )
        self.assertIn("run: python scripts/triage_reservation_e2e.py", workflow)
        self.assertIn("if-no-files-found: error", workflow)
        self.assertNotIn("continue-on-error:", workflow)

    def test_ci_runs_regressions_and_real_postgresql_without_ecr_mirror(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "validate-rules.yml").read_text(encoding="utf-8")
        self.assert_ci_contract(workflow)

    def test_triage_e2e_uses_explicit_posix_policy_with_platform_check(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "validate-rules.yml").read_text(encoding="utf-8")
        script = (ROOT / "scripts" / "triage_reservation_e2e.py").read_text(encoding="utf-8")
        self.assertIn("TRIAGE_GATEWAY_POLICY_PROFILE: posix", workflow)
        self.assertIn('policy_profile != "posix" or os.name != "posix"', script)
        self.assertIn("config/command-gateway.policy.posix.json", script)
        self.assertIn('policy.get("platform") != policy_profile', script)
        self.assertNotIn("enforce_platform=False", script)

    def test_ci_contract_detects_previous_registry_and_omitted_tests(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "validate-rules.yml").read_text(encoding="utf-8")
        for broken in (
            workflow.replace("image: postgres:16", "image: public.ecr.aws/docker/library/postgres:16"),
            workflow.replace(" tests/test_update_branch_fast_forward_governed.py", ""),
        ):
            with self.assertRaises(AssertionError):
                self.assert_ci_contract(broken)

    def test_main_redaction_remains_present(self) -> None:
        sensitive = "postgresql://synthetic:fixture-password@example.invalid/database"
        self.assertTrue(cg.sensitive_reference([sensitive], self.policy))
        self.assertEqual("[REDACTED_POSTGRES_URI]", cg.redact(sensitive))


class GovernedPr185ReconciliationPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.proposal = self.root / "proposal.json"
        self.proposal.write_text('{"fixture": true}\n', encoding="utf-8")
        self.digest = hashlib.sha256(self.proposal.read_bytes()).hexdigest()
        self.script = self.root / "reconcile_todo_gateway_pr185_main_governed.py"
        self.script.write_bytes(
            (ROOT / "scripts" / "reconcile_todo_gateway_pr185_main_governed.py").read_bytes()
        )
        self.script_digest = hashlib.sha256(self.script.read_bytes()).hexdigest()
        self.policy = cg.load_policy(
            ROOT / "config" / "command-gateway.policy.json",
            enforce_platform=False,
            validate_paths=False,
        )
        self.policy["allowed_roots"] = [str(self.repo)]
        self.policy["governed_reconciliations"] = [{
            "script": str(self.script),
            "repo_root": str(self.repo),
            "proposal": str(self.proposal),
            "proposal_sha256": self.digest,
            "script_sha256": self.script_digest,
            "confirm": "RECONCILE-TODO-GATEWAY-PR185",
        }]
        self.command = [
            "python", str(self.script),
            "--repo", str(self.repo),
            "--proposal", str(self.proposal),
            "--proposal-sha256", self.digest,
            "--confirm", "RECONCILE-TODO-GATEWAY-PR185",
            "--correlation-id", "pr185-policy-fixture",
        ]

    def test_exact_proposal_bound_invocation_is_allowed_at_risk2(self) -> None:
        cg.validate_command(self.command, 2, self.policy, self.repo)

    def test_reconciliation_requires_risk2_before_integrity_or_subprocess(self) -> None:
        for risk in (1, 3):
            with self.subTest(risk=risk):
                with (
                    mock.patch.object(cg, "sha256_file") as digest,
                    mock.patch.object(cg.subprocess, "run") as run,
                ):
                    with self.assertRaises(cg.GatewayError) as raised:
                        cg.validate_command(self.command, risk, self.policy, self.repo)
                    if risk == 1:
                        self.assertIn("requires risk 2", str(raised.exception))
                    digest.assert_not_called()
                    run.assert_not_called()

    def test_uninstalled_default_policy_has_no_reconciliation_capability(self) -> None:
        installed = cg.load_policy(
            ROOT / "config" / "command-gateway.policy.json",
            enforce_platform=False,
            validate_paths=False,
        )
        self.assertEqual(installed["governed_reconciliations"], [])
        with self.assertRaisesRegex(cg.GatewayError, "not configured"):
            cg.validate_command(self.command, 2, installed, self.repo)

    def test_all_versioned_profiles_keep_reconciliation_dormant(self) -> None:
        for name in (
            "command-gateway.policy.json",
            "command-gateway.policy.noteri-wsl.json",
            "command-gateway.policy.posix.json",
        ):
            with self.subTest(profile=name):
                profile = cg.load_policy(
                    ROOT / "config" / name,
                    enforce_platform=False,
                    validate_paths=False,
                )
                self.assertEqual(profile["governed_reconciliations"], [])

    def test_duplicate_or_broad_reconciliation_records_are_rejected(self) -> None:
        duplicate = copy.deepcopy(self.policy)
        duplicate["governed_reconciliations"].append(
            dict(duplicate["governed_reconciliations"][0])
        )
        with self.assertRaisesRegex(cg.GatewayError, "ambiguous"):
            cg.validate_command(self.command, 2, duplicate, self.repo)

        broad = copy.deepcopy(self.policy)
        broad["governed_reconciliations"][0]["repo_root"] = str(self.root / "*")
        with self.assertRaisesRegex(cg.GatewayError, "not configured"):
            cg.validate_command(self.command, 2, broad, self.repo)

    def test_proposal_path_digest_confirmation_and_argument_set_are_exact(self) -> None:
        mutations = []
        for flag, value in (
            ("--proposal", str(self.root / "other.json")),
            ("--proposal-sha256", "0" * 64),
            ("--confirm", "DIFFERENT"),
        ):
            changed = list(self.command)
            changed[changed.index(flag) + 1] = value
            mutations.append(changed)
        mutations.extend((self.command[:-2], self.command + ["--extra", "value"]))
        for command in mutations:
            with self.subTest(command=command):
                with self.assertRaises(cg.GatewayError):
                    cg.validate_command(command, 2, self.policy, self.repo)

    def test_proposal_integrity_change_is_rejected(self) -> None:
        self.proposal.write_text('{"fixture": false}\n', encoding="utf-8")
        with self.assertRaisesRegex(cg.GatewayError, "integrity"):
            cg.validate_command(self.command, 2, self.policy, self.repo)

    def test_script_integrity_change_is_rejected(self) -> None:
        self.script.write_bytes(self.script.read_bytes() + b"\n# changed\n")
        with self.assertRaisesRegex(cg.GatewayError, "script integrity"):
            cg.validate_command(self.command, 2, self.policy, self.repo)

    def test_reconciliation_is_not_available_at_risk3_or_other_worktree(self) -> None:
        with self.assertRaises(cg.GatewayError):
            cg.validate_command(self.command, 3, self.policy, self.repo)
        with self.assertRaises(cg.GatewayError):
            cg.validate_command(self.command, 2, self.policy, self.repo / "other")


if __name__ == "__main__":
    unittest.main()

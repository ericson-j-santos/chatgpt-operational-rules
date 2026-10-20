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


if __name__ == "__main__":
    unittest.main()

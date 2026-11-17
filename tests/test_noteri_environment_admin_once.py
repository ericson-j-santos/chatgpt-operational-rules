"""Off-host, credential-free tests of one exact GitHub environment update."""
from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import unittest
from copy import deepcopy
from typing import Any

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "noteri_environment_admin_once.py"
spec = importlib.util.spec_from_file_location("noteri_environment_admin_once", SCRIPT)
assert spec and spec.loader
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)

HEAD = "a" * 40


def context(**overrides: str) -> dict[str, str]:
    values = {
        "GITHUB_REPOSITORY": m.SOURCE_REPO,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_ACTOR": m.OWNER,
        "GITHUB_TRIGGERING_ACTOR": m.OWNER,
        "GITHUB_WORKFLOW_REF": f"{m.SOURCE_REPO}/{m.WORKFLOW_PATH}@refs/heads/main",
        "GITHUB_SHA": HEAD,
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_RUN_ID": "4567",
        "GITHUB_EVENT_NAME": "push",
    }
    values.update(overrides)
    return values


def env(*, bypass=False, reviewer_id=m.OWNER_ID, self_review=False,
        branch_policy=None, extra_rule=None) -> dict[str, Any]:
    rules = [{
        "type": "required_reviewers", "prevent_self_review": self_review,
        "reviewers": [{"type": "User", "reviewer": {"id": reviewer_id}}],
    }]
    if extra_rule is not None:
        rules.append(extra_rule)
    return {
        "name": m.TARGET_ENVIRONMENT,
        "id": 123,
        "can_admins_bypass": bypass,
        "protection_rules": rules,
        "deployment_branch_policy": branch_policy,
    }


class FakeAPI:
    def __init__(self, before=None, after=None, bad_repo=None, bad_owner=None):
        self.calls = []
        self.before = deepcopy(before)
        self.after = deepcopy(after)
        self.written = False
        self.bad_repo = bad_repo
        self.bad_owner = bad_owner

    def request(self, method, path, payload=None):
        self.calls.append((method, path, deepcopy(payload)))
        if method == "GET" and path == f"/repos/{m.TARGET_REPO}":
            return 200, self.bad_repo if self.bad_repo is not None else {
                "full_name": m.TARGET_REPO, "private": False,
                "default_branch": "main", "owner": {"id": m.OWNER_ID}
            }
        if method == "GET" and path == f"/users/{m.OWNER}":
            return 200, self.bad_owner if self.bad_owner is not None else {
                "login": m.OWNER, "id": m.OWNER_ID
            }
        if method == "GET" and path == m.ENV_PATH:
            item = self.after if self.written else self.before
            return (200, deepcopy(item)) if item is not None else (404, None)
        if method == "PUT" and path == m.ENV_PATH:
            self.written = True
            return 200, {"id": 123, "name": m.TARGET_ENVIRONMENT}
        raise AssertionError((method, path))

    def puts(self):
        return [x for x in self.calls if x[0] == "PUT"]


def run(api, mode="apply", confirm=m.CONFIRM, ctx=None):
    return m.reconcile(api, mode=mode, context=ctx or context(),
                       expected_rules_sha=HEAD, confirm=confirm)


class NoteriEnvironmentGuardTests(unittest.TestCase):
    def test_create_absent_environment_then_independent_readback(self):
        api = FakeAPI(after=env())
        self.assertEqual(run(api)["result"], "PROTECTION_VERIFIED")
        self.assertTrue(run(FakeAPI(before=env()))["admin_bypass_disabled"])
        self.assertEqual(len(api.puts()), 1)
        payload = api.puts()[0][2]
        self.assertEqual(payload["reviewers"], [{"type": "User", "id": m.OWNER_ID}])
        self.assertIs(payload["can_admins_bypass"], False)
        self.assertIs(payload["prevent_self_review"], False)
        self.assertIsNone(payload["deployment_branch_policy"])

    def test_idempotency_no_write_if_already_protected(self):
        api = FakeAPI(before=env())
        self.assertEqual(run(api)["result"], "PROTECTION_VERIFIED")
        self.assertFalse(run(api)["changed"])
        self.assertFalse(api.puts())

    def test_readonly_plan_does_not_write(self):
        api = FakeAPI(before=env(bypass=True))
        self.assertEqual(run(api, mode="plan")["result"], "PROTECTION_PLAN_REQUIRED")
        self.assertFalse(api.puts())

    def test_confirm_required_before_write(self):
        api = FakeAPI(before=env(bypass=True))
        with self.assertRaisesRegex(m.Blocked, "apply_confirmation_missing"):
            run(api, confirm="wrong")
        self.assertFalse(api.puts())

    def test_undocumented_bypass_write_does_not_count_as_success(self):
        api = FakeAPI(before=env(bypass=True), after=env(bypass=True))
        with self.assertRaisesRegex(m.Blocked, "write_result_unverified_or_bypass_active"):
            run(api)
        self.assertEqual(len(api.puts()), 1)

    def test_missing_bypass_field_blocks_before_write(self):
        old = env()
        del old["can_admins_bypass"]
        api = FakeAPI(before=old)
        with self.assertRaisesRegex(m.Blocked, "admin_bypass_readback_unavailable"):
            run(api)
        self.assertFalse(api.puts())

    def test_old_user_or_wrong_review_blocks_readback(self):
        api = FakeAPI(before=env(bypass=True), after=env(reviewer_id=2))
        with self.assertRaisesRegex(m.Blocked, "write_result_unverified_or_bypass_active"):
            run(api)

    def test_wrong_repository_blocks_pre_write(self):
        api = FakeAPI(bad_repo={"full_name": "x/y", "private": False, "owner": {"id": m.OWNER_ID}})
        with self.assertRaisesRegex(m.Blocked, "target_repository_not_proven_public"):
            run(api)
        self.assertFalse(api.puts())

    def test_wrong_owner_id_blocks_pre_write(self):
        api = FakeAPI(bad_owner={"login": m.OWNER, "id": 123})
        with self.assertRaisesRegex(m.Blocked, "owner_identity_mismatch"):
            run(api)
        self.assertFalse(api.puts())

    def test_existing_wait_timer_and_branch_policy_preserved(self):
        branch = {"protected_branches": False, "custom_branch_policies": True}
        before = env(bypass=True, branch_policy=branch)
        before["protection_rules"].append({"type": "wait_timer", "wait_timer": 9})
        after = env(branch_policy=branch)
        after["protection_rules"].append({"type": "wait_timer", "wait_timer": 9})
        api = FakeAPI(before=before, after=after)
        self.assertTrue(run(api)["changed"])
        self.assertEqual(api.puts()[0][2]["wait_timer"], 9)
        self.assertEqual(api.puts()[0][2]["deployment_branch_policy"], branch)

    def test_custom_protection_rule_blocks_without_overwriting(self):
        api = FakeAPI(before=env(extra_rule={"type": "custom_protection_rule", "id": 23}))
        with self.assertRaisesRegex(m.Blocked, "unrelated_protection_rules_present"):
            run(api)
        self.assertFalse(api.puts())

    def test_strict_wrong_actor_ref_sha_attempt_event(self):
        variants = [
            {"GITHUB_ACTOR": "somebot"}, {"GITHUB_TRIGGERING_ACTOR": "someone"},
            {"GITHUB_REF": "refs/heads/feature"}, {"GITHUB_SHA": "b" * 40},
            {"GITHUB_RUN_ATTEMPT": "2"}, {"GITHUB_REPOSITORY": "other/other"},
            {"GITHUB_WORKFLOW_REF": "other/workflow@refs/heads/main"},
            {"GITHUB_EVENT_NAME": "schedule"}, {"GITHUB_RUN_ID": "not-a-number"},
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                api = FakeAPI(after=env())
                with self.assertRaises(m.Blocked):
                    run(api, ctx=context(**variant))
                self.assertFalse(api.calls)

    def test_repo_name_or_environment_cannot_be_changed_by_untrusted_request(self):
        self.assertEqual(m.ENV_PATH, "/repos/ericson-j-santos/noteri-runtime/environments/noteri-worker-pool-maintenance-dev")
        with self.assertRaisesRegex(m.Blocked, "governance_token_missing_or_invalid"):
            m.GitHubAPI("")
        api = m.GitHubAPI("synthetic-test")
        with self.assertRaisesRegex(m.Blocked, "api_path_or_method_not_allowed"):
            api.request("PUT", "/repos/other/repo/environments/prod", {})

    def test_bypass_rule_true_in_future_never_falsely_completes(self):
        api = FakeAPI(before=env(bypass=True), after=env(bypass=True))
        with self.assertRaises(m.Blocked):
            run(api)
        self.assertEqual(len(api.puts()), 1)

    def test_existing_other_reviewers_must_not_be_replaced(self):
        old = env(bypass=True, reviewer_id=999)
        api = FakeAPI(before=old, after=env())
        with self.assertRaisesRegex(m.Blocked, "existing_required_reviewers_must_be_preserved"):
            run(api)
        self.assertFalse(api.puts())

    def test_existing_prevent_self_review_must_not_be_weakened(self):
        old = env(bypass=True, self_review=True)
        api = FakeAPI(before=old, after=env())
        with self.assertRaisesRegex(m.Blocked, "existing_self_review_protection_must_be_preserved"):
            run(api)
        self.assertFalse(api.puts())

    def test_no_secrets_or_host_or_process_in_workflow(self):
        workflow = pathlib.Path(__file__).resolve().parents[1] / m.WORKFLOW_PATH
        raw = workflow.read_text(encoding="utf-8")
        self.assertIn("refs/heads/main", raw)
        self.assertIn("REPOSITORY_GOVERNANCE_TOKEN: " + chr(36) + "{{ secrets.REPOSITORY_GOVERNANCE_TOKEN }}", raw)
        self.assertIn("github.triggering_actor == 'ericson-j-santos'", raw)
        self.assertIn("cancel-in-progress: false", raw)
        self.assertIn("permissions:\n  contents: read", raw)
        self.assertIn("github.sha", raw)
        self.assertNotIn("self-hosted", raw)
        self.assertNotIn("noteri_worker_pool_inplace_maintenance.py", raw)
        self.assertNotIn("command_gateway.py", raw)
        self.assertNotIn("NOTERI_POOL_MAINTENANCE_APPROVED_SHA", raw)
        self.assertNotIn("environment: noteri-worker-pool-maintenance-dev", raw)


if __name__ == "__main__":
    unittest.main()

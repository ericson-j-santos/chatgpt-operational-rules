import copy
import importlib.util
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "repository_governance_remediation.py"
SPEC = importlib.util.spec_from_file_location("repository_governance_remediation", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)

HEAD = "a" * 40
NEW_HEAD = "b" * 40


def policy():
    return {
        "schema_version": 1,
        "repositories": [
            {
                "repository": "owner/repo",
                "visibility": "public",
                "mode": "enforce",
                "default_branch": "main",
                "require_branch_protection": True,
                "require_up_to_date": True,
                "required_workflows": ["CI", "E2E"],
                "remediation": {
                    "update_branch": True,
                    "direct_merge": False,
                    "merge_queue": {
                        "enabled": True,
                        "workflow": "governed-merge-queue.yml",
                        "ref": "main",
                        "pr_input": "pr_number",
                    },
                    "branch_protection": "report_only",
                },
            }
        ],
    }


def evidence(*, decision="block", reasons=None, violations=None):
    return {
        "correlation_id": "corr-1",
        "policy_sha256": "c" * 64,
        "repositories": [
            {
                "repository": "owner/repo",
                "mode": "enforce",
                "visibility": "public",
                "status": "degraded" if violations else "healthy",
                "violations": violations or [],
                "open_pull_requests": [
                    {
                        "number": 10,
                        "head_sha": HEAD,
                        "base_ref": "main",
                        "decision": decision,
                        "reasons": reasons or [],
                    }
                ],
            }
        ],
    }


class RepositoryGovernanceRemediationTests(unittest.TestCase):
    def test_plans_update_branch_for_safe_behind_pr(self):
        plan = MODULE.plan_remediations(
            policy(),
            evidence(reasons=["behind_base", "required_gate_pending"]),
        )
        self.assertEqual(1, plan["summary"]["actions_total"])
        action = plan["actions"][0]
        self.assertEqual("update_branch", action["type"])
        self.assertEqual(HEAD, action["expected_head_sha"])

    def test_does_not_plan_update_branch_when_conflicted(self):
        plan = MODULE.plan_remediations(
            policy(),
            evidence(reasons=["behind_base", "conflict"]),
        )
        self.assertEqual([], plan["actions"])

    def test_plans_merge_queue_only_for_allow(self):
        plan = MODULE.plan_remediations(
            policy(),
            evidence(decision="allow", reasons=[]),
        )
        self.assertEqual(1, plan["summary"]["dispatch_merge_queue"])
        action = plan["actions"][0]
        self.assertEqual("dispatch_merge_queue", action["type"])
        self.assertEqual("governed-merge-queue.yml", action["workflow"])

    def test_never_plans_direct_merge(self):
        plan = MODULE.plan_remediations(
            policy(),
            evidence(decision="allow", reasons=[]),
        )
        self.assertNotIn("direct_merge", {item["type"] for item in plan["actions"]})

    def test_branch_protection_drift_becomes_blocker_not_mutation(self):
        plan = MODULE.plan_remediations(
            policy(),
            evidence(violations=["branch_unprotected"]),
        )
        self.assertTrue(plan["blockers"])
        self.assertEqual("branch_protection_drift", plan["blockers"][0]["kind"])

    def test_direct_merge_true_policy_is_flagged_unsafe(self):
        item = policy()
        item["repositories"][0]["remediation"]["direct_merge"] = True
        plan = MODULE.plan_remediations(
            item,
            evidence(decision="allow"),
        )
        self.assertTrue(
            any(blocker["kind"] == "unsafe_policy" for blocker in plan["blockers"])
        )

    def test_action_id_is_stable(self):
        first = MODULE.plan_remediations(
            policy(),
            evidence(reasons=["behind_base"]),
        )["actions"][0]["action_id"]
        second = MODULE.plan_remediations(
            policy(),
            evidence(reasons=["behind_base"]),
        )["actions"][0]["action_id"]
        self.assertEqual(first, second)

    def test_update_branch_stale_sha_is_noop(self):
        action = MODULE.plan_remediations(
            policy(),
            evidence(reasons=["behind_base"]),
        )["actions"][0]
        with mock.patch.object(
            MODULE,
            "_pull",
            return_value={
                "state": "open",
                "head": {"sha": NEW_HEAD},
                "mergeable": True,
                "mergeable_state": "clean",
            },
        ):
            result = MODULE.apply_update_branch(action, policy()["repositories"][0])
        self.assertEqual("STALE_NOOP", result["result"])
        self.assertEqual("head_sha_changed", result["reason"])

    def test_update_branch_conflict_blocks_before_mutation(self):
        action = MODULE.plan_remediations(
            policy(),
            evidence(reasons=["behind_base"]),
        )["actions"][0]
        with mock.patch.object(
            MODULE,
            "_pull",
            return_value={
                "state": "open",
                "head": {"sha": HEAD},
                "mergeable": False,
                "mergeable_state": "dirty",
            },
        ), mock.patch.object(MODULE, "run_gh") as run_gh:
            result = MODULE.apply_update_branch(action, policy()["repositories"][0])
        self.assertEqual("BLOCKED", result["result"])
        run_gh.assert_not_called()

    def test_update_branch_requires_independent_post_read(self):
        action = MODULE.plan_remediations(
            policy(),
            evidence(reasons=["behind_base"]),
        )["actions"][0]
        pulls = iter(
            [
                {
                    "state": "open",
                    "head": {"sha": HEAD},
                    "mergeable": True,
                    "mergeable_state": "clean",
                },
                {
                    "state": "open",
                    "head": {"sha": NEW_HEAD},
                    "mergeable": True,
                    "mergeable_state": "clean",
                },
            ]
        )
        comparisons = iter([{"behind_by": 2}, {"behind_by": 0}])
        with mock.patch.object(MODULE, "_pull", side_effect=lambda *a: next(pulls)),              mock.patch.object(MODULE, "_compare", side_effect=lambda *a: next(comparisons)),              mock.patch.object(MODULE, "run_gh", return_value={"message": "updating"}) as run_gh,              mock.patch.object(MODULE.time, "sleep"):
            result = MODULE.apply_update_branch(action, policy()["repositories"][0])
        self.assertEqual("UPDATED", result["result"])
        self.assertEqual(NEW_HEAD, result["current_head_sha"])
        self.assertTrue(any("--method" in call.args for call in run_gh.call_args_list))

    def test_merge_queue_blocks_draft_before_dispatch(self):
        action = MODULE.plan_remediations(
            policy(),
            evidence(decision="allow"),
        )["actions"][0]
        with mock.patch.object(
            MODULE,
            "_pull",
            return_value={
                "state": "open",
                "draft": True,
                "head": {"sha": HEAD},
                "base": {"ref": "main"},
                "mergeable": True,
                "mergeable_state": "clean",
            },
        ), mock.patch.object(MODULE, "run_gh") as run_gh:
            result = MODULE.apply_dispatch_merge_queue(
                action,
                policy()["repositories"][0],
            )
        self.assertEqual("BLOCKED", result["result"])
        self.assertEqual("draft", result["reason"])
        run_gh.assert_not_called()

    def test_merge_queue_blocks_pending_gate(self):
        action = MODULE.plan_remediations(
            policy(),
            evidence(decision="allow"),
        )["actions"][0]
        pull = {
            "state": "open",
            "draft": False,
            "head": {"sha": HEAD},
            "base": {"ref": "main"},
            "mergeable": True,
            "mergeable_state": "clean",
        }
        runs = [
            {
                "id": 1,
                "name": "CI",
                "event": "pull_request",
                "head_sha": HEAD,
                "status": "completed",
                "conclusion": "success",
                "created_at": "2026-09-22T12:00:00Z",
            },
            {
                "id": 2,
                "name": "E2E",
                "event": "pull_request",
                "head_sha": HEAD,
                "status": "in_progress",
                "conclusion": None,
                "created_at": "2026-09-22T12:00:00Z",
            },
        ]
        with mock.patch.object(MODULE, "_pull", return_value=pull),              mock.patch.object(MODULE, "_compare", return_value={"behind_by": 0}),              mock.patch.object(MODULE, "_workflow_runs", return_value=runs),              mock.patch.object(MODULE, "run_gh") as run_gh:
            result = MODULE.apply_dispatch_merge_queue(
                action,
                policy()["repositories"][0],
            )
        self.assertEqual("BLOCKED", result["result"])
        self.assertEqual("required_gate_not_green", result["reason"])
        run_gh.assert_not_called()

    def test_merge_queue_dispatches_only_after_all_rechecks(self):
        action = MODULE.plan_remediations(
            policy(),
            evidence(decision="allow"),
        )["actions"][0]
        pull = {
            "state": "open",
            "draft": False,
            "head": {"sha": HEAD},
            "base": {"ref": "main"},
            "mergeable": True,
            "mergeable_state": "clean",
        }
        runs = [
            {
                "id": 1,
                "name": "CI",
                "event": "pull_request",
                "head_sha": HEAD,
                "status": "completed",
                "conclusion": "success",
                "created_at": "2026-09-22T12:00:00Z",
            },
            {
                "id": 2,
                "name": "E2E",
                "event": "pull_request",
                "head_sha": HEAD,
                "status": "completed",
                "conclusion": "success",
                "created_at": "2026-09-22T12:00:00Z",
            },
        ]
        before = {"workflow_runs": [{"id": 10, "head_branch": "main"}]}
        after = {
            "workflow_runs": [
                {
                    "id": 11,
                    "head_branch": "main",
                    "html_url": "https://example.invalid/run/11",
                    "status": "queued",
                },
                {"id": 10, "head_branch": "main"},
            ]
        }
        api_outputs = iter([before, None, after])
        with mock.patch.object(MODULE, "_pull", return_value=pull),              mock.patch.object(MODULE, "_compare", return_value={"behind_by": 0}),              mock.patch.object(MODULE, "_workflow_runs", return_value=runs),              mock.patch.object(MODULE, "run_gh", side_effect=lambda *a, **k: next(api_outputs)):
            result = MODULE.apply_dispatch_merge_queue(
                action,
                policy()["repositories"][0],
            )
        self.assertEqual("DISPATCHED", result["result"])
        self.assertEqual(11, result["run_id"])

    def test_apply_plan_rejects_repo_not_in_policy(self):
        item = policy()
        plan = {
            "actions": [
                {
                    "action_id": "x",
                    "type": "update_branch",
                    "repository": "other/repo",
                    "pr_number": 1,
                    "expected_head_sha": HEAD,
                }
            ]
        }
        result = MODULE.apply_plan(item, plan)
        self.assertEqual("BLOCKED", result["results"][0]["result"])
        self.assertEqual("repository_not_in_policy", result["results"][0]["reason"])


    def test_scheduled_workflow_applies_only_safe_branch_refresh(self):
        workflow = (
            ROOT / ".github/workflows/repository-governance-control-plane.yml"
        ).read_text(encoding="utf-8")
        assert "Restrict scheduled remediation to safe branch refresh" in workflow
        assert 'item.get("type") == "update_branch"' in workflow
        assert "Apply scheduled safe remediations" in workflow
        assert "REPOSITORY_GOVERNANCE_TOKEN" in workflow

    def test_powerbi_policy_enforces_fresh_branch_without_merge_queue(self):
        import json

        payload = json.loads(
            (ROOT / "config/repository-governance-control-plane.json").read_text(
                encoding="utf-8"
            )
        )
        powerbi = next(
            item
            for item in payload["repositories"]
            if item["repository"] == "ericson-j-santos/powerbi-platform"
        )
        self.assertEqual("enforce", powerbi["mode"])
        self.assertTrue(powerbi["require_up_to_date"])
        self.assertTrue(powerbi["remediation"]["update_branch"])
        self.assertFalse(powerbi["remediation"]["direct_merge"])
        self.assertFalse(powerbi["remediation"]["merge_queue"]["enabled"])
        self.assertEqual(
            ["Validate Power BI repository", "Power BI Desktop E2E"],
            powerbi["required_workflows"],
        )



if __name__ == "__main__":
    unittest.main()

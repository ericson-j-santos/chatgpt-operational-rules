import copy
import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "repository_governance_control_plane.py"
SPEC = importlib.util.spec_from_file_location("repository_governance_control_plane", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)

HEAD = "a" * 40
MAIN = "b" * 40


def run(name, state="success", created_at="2026-09-22T12:00:00Z", run_id=1):
    status = "completed" if state != "pending" else "in_progress"
    conclusion = state if state not in {"pending", "missing"} else None
    return {
        "id": run_id,
        "name": name,
        "event": "pull_request",
        "head_sha": HEAD,
        "status": status,
        "conclusion": conclusion,
        "created_at": created_at,
        "updated_at": created_at,
        "html_url": f"https://example.invalid/runs/{run_id}",
    }


def policy(required=None, protected=True, require_status_checks=False):
    return {
        "repository": "owner/repo",
        "visibility": "public",
        "mode": "enforce",
        "default_branch": "main",
        "require_branch_protection": protected,
        "require_required_status_checks": require_status_checks,
        "require_up_to_date": True,
        "required_workflows": required or ["CI", "E2E"],
    }


def raw_pull(
    *,
    number=1,
    draft=False,
    mergeable=True,
    mergeable_state="clean",
    behind_by=0,
    runs=None,
):
    return {
        "detail": {
            "number": number,
            "title": "PR",
            "html_url": f"https://example.invalid/pull/{number}",
            "draft": draft,
            "mergeable": mergeable,
            "mergeable_state": mergeable_state,
            "head": {"sha": HEAD, "ref": "feature"},
            "base": {"ref": "main"},
        },
        "comparison": {"behind_by": behind_by},
        "workflow_runs": runs
        if runs is not None
        else [run("CI", run_id=1), run("E2E", run_id=2)],
    }


def raw_repo(*, protected=True, pulls=None, rulesets=None, rulesets_error=None):
    return {
        "repository": "owner/repo",
        "metadata": {
            "default_branch": "main",
            "archived": False,
            "allow_auto_merge": True,
        },
        "branch": {
            "protected": protected,
            "commit": {"sha": MAIN},
        },
        "rulesets": rulesets,
        "rulesets_error": rulesets_error,
        "pulls": pulls or [],
    }


def ruleset_with_required_checks(*contexts):
    return {
        "id": 1,
        "enforcement": "active",
        "conditions": {
            "ref_name": {
                "include": ["~DEFAULT_BRANCH"],
                "exclude": [],
            }
        },
        "rules": [
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [
                        {"context": context} for context in contexts
                    ]
                },
            }
        ],
    }


class RepositoryGovernanceControlPlaneTests(unittest.TestCase):
    def test_canonical_repo_is_enforced_with_exact_mandatory_context(self):
        config = json.loads(
            (ROOT / "config" / "repository-governance-control-plane.json").read_text(
                encoding="utf-8"
            )
        )
        MODULE.validate_policy(config)
        matching = [
            item for item in config["repositories"]
            if item["repository"] == "ericson-j-santos/chatgpt-operational-rules"
        ]
        self.assertEqual(1, len(matching))
        item = matching[0]
        self.assertEqual("public", item["visibility"])
        self.assertEqual("enforce", item["mode"])
        self.assertEqual("main", item["default_branch"])
        self.assertTrue(item["require_branch_protection"])
        self.assertTrue(item["require_up_to_date"])
        self.assertTrue(item["require_required_status_checks"])
        self.assertEqual(
            ["Rules, Gateway, Session Bootstrap and E2E gate"],
            item["expected_status_check_contexts"],
        )
        self.assertEqual(["Validate Operational Rules"], item["required_workflows"])
        self.assertIs(item["remediation"]["direct_merge"], False)
        self.assertIs(item["remediation"]["update_branch"], False)
        self.assertEqual("provider_capability_blocked", item["remediation"]["branch_protection"])
        unprotected = MODULE.evaluate_repository(item, raw_repo(protected=False, rulesets=[]))
        self.assertEqual("degraded", unprotected["status"])
        self.assertIn("branch_unprotected", unprotected["violations"])
        self.assertIn("required_status_checks_missing", unprotected["violations"])

    def test_wrong_required_check_name_is_not_false_green(self):
        item = policy(protected=True, require_status_checks=True)
        item["expected_status_check_contexts"] = [
            "Rules, Gateway, Session Bootstrap and E2E gate"
        ]
        mismatched = MODULE.evaluate_repository(
            item, raw_repo(protected=True, rulesets=[ruleset_with_required_checks("Other green check")])
        )
        self.assertEqual("degraded", mismatched["status"])
        self.assertIn("required_status_check_contexts_missing", mismatched["violations"])
        self.assertTrue(mismatched["required_status_checks_enforced"])
        self.assertEqual(["Other green check"], mismatched["required_status_checks"])

        matched = MODULE.evaluate_repository(
            item, raw_repo(
                protected=True,
                rulesets=[ruleset_with_required_checks(
                    "Rules, Gateway, Session Bootstrap and E2E gate"
                )],
            ),
        )
        self.assertEqual("healthy", matched["status"])
        self.assertEqual([], matched["violations"])

    def test_expected_context_policy_rejects_invalid_or_unsafe_cases(self):
        baseline = policy(protected=True, require_status_checks=True)
        for bad in [[""], ["CI", "CI"], "CI", [None]]:
            case = dict(baseline)
            case["expected_status_check_contexts"] = bad
            with self.subTest(bad=bad), self.assertRaises(MODULE.GovernanceControlPlaneError):
                MODULE.validate_policy({"schema_version": 1, "repositories": [case]})

        disabled = dict(baseline)
        disabled["require_required_status_checks"] = False
        disabled["expected_status_check_contexts"] = ["CI"]
        with self.assertRaises(MODULE.GovernanceControlPlaneError):
            MODULE.validate_policy({"schema_version": 1, "repositories": [disabled]})

    def test_policy_rejects_duplicate_repository(self):
        item = policy()
        document = {"schema_version": 1, "repositories": [item, copy.deepcopy(item)]}
        with self.assertRaises(MODULE.GovernanceControlPlaneError):
            MODULE.validate_policy(document)

    def test_latest_required_workflow_state_uses_current_head(self):
        states = MODULE.latest_required_workflow_states(
            [
                {**run("CI", run_id=1), "head_sha": "c" * 40},
                run("CI", created_at="2026-09-22T12:01:00Z", run_id=2),
                run("E2E", run_id=3),
            ],
            ["CI", "E2E"],
            head_sha=HEAD,
        )
        self.assertEqual(["success", "success"], [item["state"] for item in states])
        self.assertEqual(2, states[0]["run_id"])

    def test_missing_gate_is_explicit(self):
        states = MODULE.latest_required_workflow_states(
            [run("CI")],
            ["CI", "E2E"],
            head_sha=HEAD,
        )
        self.assertEqual("missing", states[1]["state"])

    def test_green_current_head_can_be_allowed(self):
        result = MODULE.evaluate_pull(policy(), raw_pull())
        self.assertEqual("allow", result["decision"])
        self.assertEqual([], result["reasons"])

    def test_draft_is_hold_even_with_green_gates(self):
        result = MODULE.evaluate_pull(policy(), raw_pull(draft=True))
        self.assertEqual("hold", result["decision"])
        self.assertIn("draft", result["reasons"])

    def test_pending_gate_is_hold(self):
        result = MODULE.evaluate_pull(
            policy(),
            raw_pull(runs=[run("CI"), run("E2E", state="pending", run_id=2)]),
        )
        self.assertEqual("hold", result["decision"])
        self.assertIn("required_gate_pending", result["reasons"])

    def test_failed_or_missing_gate_blocks(self):
        failed = MODULE.evaluate_pull(
            policy(),
            raw_pull(runs=[run("CI", state="failure"), run("E2E", run_id=2)]),
        )
        missing = MODULE.evaluate_pull(
            policy(),
            raw_pull(runs=[run("CI")]),
        )
        self.assertEqual("block", failed["decision"])
        self.assertEqual("block", missing["decision"])

    def test_conflict_blocks(self):
        result = MODULE.evaluate_pull(
            policy(),
            raw_pull(mergeable=False, mergeable_state="dirty"),
        )
        self.assertEqual("block", result["decision"])
        self.assertIn("conflict", result["reasons"])

    def test_behind_base_blocks(self):
        result = MODULE.evaluate_pull(policy(), raw_pull(behind_by=2))
        self.assertEqual("block", result["decision"])
        self.assertIn("behind_base", result["reasons"])

    def test_unprotected_required_branch_degrades_repository(self):
        result = MODULE.evaluate_repository(
            policy(protected=True),
            raw_repo(protected=False),
        )
        self.assertEqual("degraded", result["status"])
        self.assertIn("branch_unprotected", result["violations"])

    def test_required_status_checks_missing_degrades_repository(self):
        result = MODULE.evaluate_repository(
            policy(protected=True, require_status_checks=True),
            raw_repo(protected=True, rulesets=[]),
        )
        self.assertEqual("degraded", result["status"])
        self.assertFalse(result["required_status_checks_enforced"])
        self.assertIn("required_status_checks_missing", result["violations"])

    def test_required_status_checks_active_keeps_repository_healthy(self):
        result = MODULE.evaluate_repository(
            policy(protected=True, require_status_checks=True),
            raw_repo(
                protected=True,
                rulesets=[ruleset_with_required_checks("CI", "PR Evidence Gate")],
            ),
        )
        self.assertEqual("healthy", result["status"])
        self.assertTrue(result["required_status_checks_enforced"])
        self.assertEqual(["CI", "PR Evidence Gate"], result["required_status_checks"])

    def test_classic_branch_protection_with_exact_context_is_healthy(self):
        expected = "Rules, Gateway, Session Bootstrap and E2E gate"
        item = policy(protected=True, require_status_checks=True)
        item["expected_status_check_contexts"] = [expected]
        old_style = raw_repo(protected=True, rulesets=[])
        old_style["branch"]["protection"] = {
            "enabled": True,
            "required_status_checks": {
                "enforcement_level": "everyone",
                "contexts": [expected],
                "checks": [{"context": expected}],
            },
        }
        result = MODULE.evaluate_repository(item, old_style)
        self.assertEqual("healthy", result["status"])
        self.assertTrue(result["required_status_checks_enforced"])
        self.assertEqual([expected], result["required_status_checks"])

    def test_classic_branch_wrong_check_and_disabled_protection_fail_closed(self):
        expected = "Rules, Gateway, Session Bootstrap and E2E gate"
        item = policy(protected=True, require_status_checks=True)
        item["expected_status_check_contexts"] = [expected]
        for level in ("off", "", None):
            source = raw_repo(protected=True, rulesets=[])
            source["branch"]["protection"] = {
                "enabled": True,
                "required_status_checks": {
                    "enforcement_level": level, "contexts": [expected], "checks": []
                },
            }
            result = MODULE.evaluate_repository(item, source)
            with self.subTest(level=level):
                self.assertIn("required_status_checks_missing", result["violations"])
                self.assertFalse(result["required_status_checks_enforced"])

        mismatched = raw_repo(protected=True, rulesets=[])
        mismatched["branch"]["protection"] = {
            "enabled": True,
            "required_status_checks": {
                "enforcement_level": "everyone", "contexts": ["Other check"], "checks": []
            },
        }
        result = MODULE.evaluate_repository(item, mismatched)
        self.assertIn("required_status_check_contexts_missing", result["violations"])

        disabled = raw_repo(protected=True, rulesets=[])
        disabled["branch"]["protection"] = {
            "enabled": False,
            "required_status_checks": {
                "enforcement_level": "everyone", "contexts": [expected], "checks": []
            },
        }
        self.assertIn(
            "required_status_checks_missing", MODULE.evaluate_repository(item, disabled)["violations"]
        )

    def test_classic_protection_readable_even_if_ruleset_api_forbidden(self):
        expected = "Rules, Gateway, Session Bootstrap and E2E gate"
        item = policy(protected=True, require_status_checks=True)
        item["expected_status_check_contexts"] = [expected]
        source = raw_repo(
            protected=True, rulesets=None, rulesets_error="403 Resource not accessible"
        )
        source["branch"]["protection"] = {
            "enabled": True,
            "required_status_checks": {
                "enforcement_level": "everyone", "contexts": [], "checks": [{"context": expected}]
            },
        }
        result = MODULE.evaluate_repository(item, source)
        self.assertEqual("healthy", result["status"])
        self.assertEqual([expected], result["required_status_checks"])

    def test_required_status_checks_unverifiable_degrades_repository(self):
        result = MODULE.evaluate_repository(
            policy(protected=True, require_status_checks=True),
            raw_repo(
                protected=True,
                rulesets=None,
                rulesets_error="403 Resource not accessible",
            ),
        )
        self.assertEqual("degraded", result["status"])
        self.assertIn("required_status_checks_unverifiable", result["violations"])

    def test_observe_repo_can_be_healthy_without_required_workflows(self):
        item = policy(required=[], protected=False)
        item["mode"] = "observe"
        result = MODULE.evaluate_repository(item, raw_repo(protected=False))
        self.assertEqual("healthy", result["status"])

    def test_private_skipped_repo_is_not_invented_as_audited(self):
        public_policy = policy()
        private_policy = copy.deepcopy(policy())
        private_policy["repository"] = "owner/private"
        private_policy["visibility"] = "private"
        document = {
            "schema_version": 1,
            "repositories": [public_policy, private_policy],
        }
        snapshot = {
            "schema_version": 1,
            "generated_at": "2026-09-22T12:00:00Z",
            "correlation_id": "corr-test",
            "repositories": [raw_repo()],
            "skipped": [
                {
                    "repository": "owner/private",
                    "mode": "enforce",
                    "status": "skipped_private",
                    "reason": "cross_repository_token_unavailable",
                }
            ],
        }
        result = MODULE.evaluate_control_plane(document, snapshot)
        self.assertEqual(1, result["summary"]["repositories_total"])
        self.assertEqual("owner/private", result["skipped"][0]["repository"])

    def test_snapshot_missing_repo_is_unreachable_not_success(self):
        document = {"schema_version": 1, "repositories": [policy()]}
        snapshot = {
            "schema_version": 1,
            "repositories": [],
            "skipped": [],
        }
        result = MODULE.evaluate_control_plane(document, snapshot)
        self.assertEqual("unreachable", result["repositories"][0]["status"])
        self.assertEqual(1, result["summary"]["repositories_unreachable"])


if __name__ == "__main__":
    unittest.main()

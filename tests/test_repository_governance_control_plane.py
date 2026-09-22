import copy
import importlib.util
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


def policy(required=None, protected=True):
    return {
        "repository": "owner/repo",
        "visibility": "public",
        "mode": "enforce",
        "default_branch": "main",
        "require_branch_protection": protected,
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


def raw_repo(*, protected=True, pulls=None):
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
        "pulls": pulls or [],
    }


class RepositoryGovernanceControlPlaneTests(unittest.TestCase):
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

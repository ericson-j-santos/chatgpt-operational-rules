import json
import unittest
from unittest.mock import patch

from scripts.dispatch_workflow_governed import dispatch, validate


REPO = "ericson-j-santos/reqsys-v2-enterprise-real"
WF = "planner-teams-notify-dev-acceptance.yml"
WEEKLY_WF = "reqsys-weekly-accomplishment-log.yml"
TODO_HOURLY_REPO = "ericson-j-santos/chatgpt-operational-rules"
TODO_HOURLY_WF = "todo-global-hourly-cycle.yml"


class DispatchWorkflowGovernedTests(unittest.TestCase):
    def test_rejects_repo_outside_allowlist(self):
        with self.assertRaisesRegex(ValueError, "repositório"):
            validate("other/repo", WF, "main")

    def test_rejects_workflow_outside_allowlist(self):
        with self.assertRaisesRegex(ValueError, "workflow"):
            validate(REPO, "danger.yml", "main")

    def test_accepts_reqsys_weekly_accomplishment_workflow_on_main(self):
        self.assertEqual(
            validate(REPO, WEEKLY_WF, "main"),
            (REPO, WEEKLY_WF, "main"),
        )

    def test_accepts_todo_global_hourly_workflow_on_main(self):
        self.assertEqual(
            validate(TODO_HOURLY_REPO, TODO_HOURLY_WF, "main"),
            (TODO_HOURLY_REPO, TODO_HOURLY_WF, "main"),
        )

    def test_rejects_ref_outside_allowlist(self):
        with self.assertRaisesRegex(ValueError, "ref"):
            validate(REPO, WF, "prod")

    def test_dispatch_confirms_new_run_id(self):
        before = json.dumps({"workflow_runs": [{"id": 10, "head_branch": "main"}]})
        after = json.dumps({
            "workflow_runs": [
                {
                    "id": 11,
                    "head_branch": "main",
                    "html_url": "https://example/run/11",
                    "status": "queued",
                },
                {"id": 10, "head_branch": "main"},
            ]
        })
        outputs = iter([before, "", after])
        with patch(
            "scripts.dispatch_workflow_governed.run",
            side_effect=lambda *args: next(outputs),
        ):
            result = dispatch(REPO, WF, "main", "corr-1")

        self.assertEqual(result["result"], "DISPATCHED")
        self.assertEqual(result["run_id"], 11)
        self.assertEqual(result["correlation_id"], "corr-1")

    def test_dispatch_retries_confirmation_without_repeating_post(self):
        before = json.dumps({"workflow_runs": [{"id": 10, "head_branch": "main"}]})
        empty = json.dumps({"workflow_runs": [{"id": 10, "head_branch": "main"}]})
        visible = json.dumps({
            "workflow_runs": [
                {
                    "id": 11,
                    "head_branch": "main",
                    "html_url": "https://example/run/11",
                    "status": "queued",
                },
                {"id": 10, "head_branch": "main"},
            ]
        })
        outputs = iter([before, "", empty, visible])
        calls = []

        def fake_run(*args):
            calls.append(args)
            return next(outputs)

        with patch(
            "scripts.dispatch_workflow_governed.run",
            side_effect=fake_run,
        ), patch("scripts.dispatch_workflow_governed.time.sleep") as sleep:
            result = dispatch(REPO, WEEKLY_WF, "main", "corr-retry")

        post_calls = [
            args for args in calls if "--method" in args and "POST" in args
        ]
        self.assertEqual(len(post_calls), 1)
        sleep.assert_called_once_with(1)
        self.assertEqual(result["run_id"], 11)
        self.assertEqual(result["correlation_id"], "corr-retry")

    def test_dispatch_stops_after_bounded_confirmation_attempts(self):
        before = json.dumps({"workflow_runs": [{"id": 10, "head_branch": "main"}]})
        empty = json.dumps({"workflow_runs": [{"id": 10, "head_branch": "main"}]})
        outputs = iter([before, "", empty, empty, empty])

        with patch(
            "scripts.dispatch_workflow_governed.DISPATCH_CONFIRM_ATTEMPTS",
            3,
        ), patch(
            "scripts.dispatch_workflow_governed.run",
            side_effect=lambda *args: next(outputs),
        ), patch("scripts.dispatch_workflow_governed.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "retentativas limitadas"):
                dispatch(REPO, WEEKLY_WF, "main", "corr-timeout")

        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [1, 2],
        )


if __name__ == "__main__":
    unittest.main()

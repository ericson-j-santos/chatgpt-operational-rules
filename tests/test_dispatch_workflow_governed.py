import json
import subprocess
import unittest
from unittest.mock import patch

from scripts.dispatch_workflow_governed import (
    NOTERI_EVENTLOG_TARGET,
    REQSYS_REFS,
    REQSYS_REPO,
    REQSYS_WORKFLOWS,
    TODO22_MAIN_TARGET,
    TODO22_TARGET,
    dispatch,
    run,
    validate,
)


SHA = "a" * 40
OTHER_SHA = "b" * 40
WF = "planner-teams-notify-dev-acceptance.yml"
WEEKLY_WF = "reqsys-weekly-accomplishment-log.yml"


def _remote_ref(sha=SHA):
    return json.dumps({"object": {"sha": sha}})


def _todo22_pr(sha=SHA, *, state="open", draft=True, ref=TODO22_TARGET[2]):
    return {
        "number": 999,
        "state": state,
        "draft": draft,
        "base": {"ref": "main"},
        "head": {
            "ref": ref,
            "sha": sha,
            "repo": {"full_name": TODO22_TARGET[0]},
        },
    }


def _todo22_pulls(*pulls):
    return json.dumps(list(pulls))


def _runs(*items):
    return json.dumps({"workflow_runs": list(items)})


def _run_item(run_id, sha=SHA, ref="main", display_title=None):
    item = {
        "id": run_id,
        "head_branch": ref,
        "head_sha": sha,
        "html_url": f"https://example/run/{run_id}",
        "status": "queued",
    }
    if display_title is not None:
        item["display_title"] = display_title
    return item


class DispatchWorkflowGovernedTests(unittest.TestCase):
    def test_rejects_target_outside_exact_allowlist(self):
        with self.assertRaisesRegex(ValueError, "allowlist"):
            validate("other/repo", WF, "main", SHA)

    def test_accepts_every_existing_reqsys_combination(self):
        for workflow in REQSYS_WORKFLOWS:
            for ref in REQSYS_REFS:
                with self.subTest(workflow=workflow, ref=ref):
                    self.assertEqual(
                        validate(REQSYS_REPO, workflow, ref, SHA),
                        (REQSYS_REPO, workflow, ref, SHA),
                    )

    def test_accepts_only_exact_todo22_targets(self):
        for target in (TODO22_TARGET, TODO22_MAIN_TARGET):
            with self.subTest(target=target):
                self.assertEqual(validate(*target, SHA), (*target, SHA))
        rejected = [
            (TODO22_TARGET[0], WF, TODO22_TARGET[2]),
            (REQSYS_REPO, TODO22_TARGET[1], "main"),
            (TODO22_TARGET[0], TODO22_TARGET[1], "feature/not-allowlisted"),
            (TODO22_TARGET[0], TODO22_TARGET[1], next(iter(REQSYS_REFS - {"main"}))),
        ]
        for target in rejected:
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "allowlist"):
                validate(*target, SHA)

    def test_accepts_only_exact_noteri_eventlog_target(self):
        self.assertEqual(
            validate(*NOTERI_EVENTLOG_TARGET, SHA),
            (*NOTERI_EVENTLOG_TARGET, SHA),
        )
        repo, workflow, ref = NOTERI_EVENTLOG_TARGET
        rejected = [
            ("other/noteri-runtime", workflow, ref),
            (repo, "other-workflow.yml", ref),
            (repo, workflow, "feature/not-main"),
            (repo, workflow, "refs/heads/main"),
        ]
        for target in rejected:
            with self.subTest(target=target), self.assertRaisesRegex(
                ValueError, "allowlist"
            ):
                validate(*target, SHA)

    def test_noteri_eventlog_dispatch_has_only_controlled_inputs_and_exact_head(self):
        title = "Noteri SQL EventLog / noteri-eventlog-corr"
        before = _runs(_run_item(40))
        after = _runs(_run_item(41, display_title=title), _run_item(40))
        outputs = iter([_remote_ref(), before, "", after])
        calls = []

        def fake_run(*args):
            calls.append(args)
            return next(outputs)

        with patch(
            "scripts.dispatch_workflow_governed.run", side_effect=fake_run
        ):
            result = dispatch(
                *NOTERI_EVENTLOG_TARGET,
                "noteri-eventlog-corr",
                SHA,
            )

        post = next(args for args in calls if "--method" in args and "POST" in args)
        self.assertEqual(
            post,
            (
                "gh", "api", "--method", "POST",
                "repos/ericson-j-santos/noteri-runtime/actions/workflows/"
                "noteri-sql-application-eventlog.yml/dispatches",
                "-f", "ref=main",
                "-f", f"inputs[expected_sha]={SHA}",
                "-f", "inputs[correlation_id]=noteri-eventlog-corr",
            ),
        )
        self.assertEqual(result["head_sha"], SHA)
        self.assertEqual(result["run_id"], 41)

    def test_noteri_eventlog_correlation_mismatch_fails_closed(self):
        before = _runs(_run_item(50))
        wrong_title = _runs(
            _run_item(51, display_title="Noteri SQL EventLog / other-correlation"),
            _run_item(50),
        )
        outputs = iter([_remote_ref(), before, "", wrong_title])
        calls = []

        def fake_run(*args):
            calls.append(args)
            return next(outputs)

        with patch(
            "scripts.dispatch_workflow_governed.DISPATCH_CONFIRM_ATTEMPTS", 1
        ), patch(
            "scripts.dispatch_workflow_governed.run", side_effect=fake_run
        ):
            with self.assertRaisesRegex(RuntimeError, "retentativas limitadas"):
                dispatch(
                    *NOTERI_EVENTLOG_TARGET,
                    "noteri-eventlog-corr",
                    SHA,
                )

        post_calls = [args for args in calls if "--method" in args and "POST" in args]
        self.assertEqual(len(post_calls), 1)

    def test_rejects_invalid_expected_head(self):
        with self.assertRaisesRegex(ValueError, "HEAD"):
            validate(REQSYS_REPO, WF, "main", "short")

    def test_rejects_invalid_correlation_identifier_before_remote_calls(self):
        with patch("scripts.dispatch_workflow_governed.run") as mocked:
            with self.assertRaisesRegex(ValueError, "correlação"):
                dispatch(REQSYS_REPO, WF, "main", "bad\nvalue", SHA)
        mocked.assert_not_called()

    def test_dispatch_confirms_exact_new_run_and_posts_once(self):
        before = _runs(_run_item(10))
        after = _runs(_run_item(11), _run_item(10))
        outputs = iter([_remote_ref(), before, "", after])
        calls = []

        def fake_run(*args):
            calls.append(args)
            return next(outputs)

        with patch("scripts.dispatch_workflow_governed.run", side_effect=fake_run):
            result = dispatch(REQSYS_REPO, WF, "main", "corr-1", SHA)

        post_calls = [args for args in calls if "--method" in args and "POST" in args]
        self.assertEqual(len(post_calls), 1)
        self.assertFalse(any("inputs[" in arg for arg in post_calls[0]))
        self.assertEqual(result["result"], "DISPATCHED")
        self.assertEqual(result["run_id"], 11)
        self.assertEqual(result["head_sha"], SHA)
        self.assertEqual(result["correlation_id"], "corr-1")

    def test_todo22_dispatch_is_bound_to_open_pr_ref_repo_base_and_head(self):
        ref = TODO22_TARGET[2]
        title = "Operational Rules / todo22-corr"
        before = _runs(_run_item(20, ref=ref))
        after = _runs(
            _run_item(21, ref=ref, display_title=title),
            _run_item(20, ref=ref),
        )
        outputs = iter([_todo22_pulls(_todo22_pr()), before, "", after])
        calls = []

        def fake_run(*args):
            calls.append(args)
            return next(outputs)

        with patch("scripts.dispatch_workflow_governed.run", side_effect=fake_run):
            result = dispatch(*TODO22_TARGET, "todo22-corr", SHA)

        self.assertIn(
            "repos/ericson-j-santos/chatgpt-operational-rules/pulls?state=open",
            calls[0][-1],
        )
        self.assertIn("head=ericson-j-santos%3Afix%2Ftodo22", calls[0][-1])
        post = next(args for args in calls if "--method" in args and "POST" in args)
        self.assertIn(f"inputs[expected_sha]={SHA}", post)
        self.assertIn("inputs[correlation_id]=todo22-corr", post)
        self.assertEqual(result["run_id"], 21)
        self.assertEqual(result["head_sha"], SHA)
        self.assertEqual(result["pull_request"], 999)

    def test_todo22_main_dispatch_is_bound_to_exact_remote_head(self):
        title = "Operational Rules / todo22-main-corr"
        before = _runs(_run_item(30))
        after = _runs(
            _run_item(31, display_title=title),
            _run_item(30),
        )
        outputs = iter([_remote_ref(), before, "", after])
        calls = []

        def fake_run(*args):
            calls.append(args)
            return next(outputs)

        with patch("scripts.dispatch_workflow_governed.run", side_effect=fake_run):
            result = dispatch(*TODO22_MAIN_TARGET, "todo22-main-corr", SHA)

        self.assertIn(
            "repos/ericson-j-santos/chatgpt-operational-rules/git/ref/heads%2Fmain",
            calls[0][-1],
        )
        post = next(args for args in calls if "--method" in args and "POST" in args)
        self.assertIn(f"inputs[expected_sha]={SHA}", post)
        self.assertIn("inputs[correlation_id]=todo22-main-corr", post)
        self.assertEqual(result["run_id"], 31)
        self.assertEqual(result["head_sha"], SHA)
        self.assertNotIn("pull_request", result)

    def test_todo22_main_wrong_head_blocks_before_post(self):
        with patch(
            "scripts.dispatch_workflow_governed.run",
            return_value=_remote_ref(OTHER_SHA),
        ) as mocked:
            with self.assertRaisesRegex(RuntimeError, "remote HEAD"):
                dispatch(*TODO22_MAIN_TARGET, "todo22-main-corr", SHA)
        self.assertEqual(mocked.call_count, 1)

    def test_todo22_pr_mismatch_blocks_before_post(self):
        with patch(
            "scripts.dispatch_workflow_governed.run",
            return_value=_todo22_pulls(_todo22_pr(OTHER_SHA)),
        ) as mocked:
            with self.assertRaisesRegex(RuntimeError, "pull request target"):
                dispatch(*TODO22_TARGET, "todo22-corr", SHA)
        self.assertEqual(mocked.call_count, 1)

    def test_todo22_non_draft_pr_blocks_before_post(self):
        with patch(
            "scripts.dispatch_workflow_governed.run",
            return_value=_todo22_pulls(_todo22_pr(draft=False)),
        ) as mocked:
            with self.assertRaisesRegex(RuntimeError, "pull request target"):
                dispatch(*TODO22_TARGET, "todo22-corr", SHA)
        self.assertEqual(mocked.call_count, 1)

    def test_todo22_requires_exactly_one_open_draft_pr(self):
        for response in (
            _todo22_pulls(),
            _todo22_pulls(_todo22_pr(), _todo22_pr()),
        ):
            with self.subTest(response=response), patch(
                "scripts.dispatch_workflow_governed.run",
                return_value=response,
            ) as mocked:
                with self.assertRaisesRegex(RuntimeError, "unique draft"):
                    dispatch(*TODO22_TARGET, "todo22-corr", SHA)
            self.assertEqual(mocked.call_count, 1)

    def test_legacy_remote_head_mismatch_blocks_before_post(self):
        with patch(
            "scripts.dispatch_workflow_governed.run",
            return_value=_remote_ref(OTHER_SHA),
        ) as mocked:
            with self.assertRaisesRegex(RuntimeError, "remote HEAD"):
                dispatch(REQSYS_REPO, WF, "main", "legacy-corr", SHA)
        self.assertEqual(mocked.call_count, 1)

    def test_dispatch_retries_confirmation_without_repeating_post(self):
        before = _runs(_run_item(10))
        empty = _runs(_run_item(10))
        visible = _runs(_run_item(11), _run_item(10))
        outputs = iter([_remote_ref(), before, "", empty, visible])
        calls = []

        def fake_run(*args):
            calls.append(args)
            return next(outputs)

        with patch(
            "scripts.dispatch_workflow_governed.run",
            side_effect=fake_run,
        ), patch("scripts.dispatch_workflow_governed.time.sleep") as sleep:
            result = dispatch(REQSYS_REPO, WEEKLY_WF, "main", "corr-retry", SHA)

        post_calls = [args for args in calls if "--method" in args and "POST" in args]
        self.assertEqual(len(post_calls), 1)
        sleep.assert_called_once_with(1)
        self.assertEqual(result["run_id"], 11)

    def test_wrong_head_sha_is_ignored_then_confirmation_times_out(self):
        before = _runs(_run_item(10))
        wrong = _runs(_run_item(11, OTHER_SHA), _run_item(10))
        outputs = iter([_remote_ref(), before, "", wrong, wrong, wrong])
        with patch(
            "scripts.dispatch_workflow_governed.DISPATCH_CONFIRM_ATTEMPTS",
            3,
        ), patch(
            "scripts.dispatch_workflow_governed.run",
            side_effect=lambda *args: next(outputs),
        ), patch("scripts.dispatch_workflow_governed.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "retentativas limitadas"):
                dispatch(REQSYS_REPO, WEEKLY_WF, "main", "corr-timeout", SHA)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    def test_ambiguous_concurrent_runs_fail_closed(self):
        before = _runs(_run_item(10))
        ambiguous = _runs(_run_item(11), _run_item(12), _run_item(10))
        outputs = iter([_remote_ref(), before, "", ambiguous])
        with patch(
            "scripts.dispatch_workflow_governed.run",
            side_effect=lambda *args: next(outputs),
        ):
            with self.assertRaisesRegex(RuntimeError, "ambiguous"):
                dispatch(REQSYS_REPO, WF, "main", "corr-ambiguous", SHA)

    def test_command_failure_does_not_echo_remote_output(self):
        completed = subprocess.CompletedProcess(
            ["gh", "api", "endpoint"],
            1,
            stdout="sensitive-response",
            stderr="token=secret-value",
        )
        with patch("scripts.dispatch_workflow_governed.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "github command failed rc=1") as raised:
                run("gh", "api", "endpoint")
        self.assertNotIn("secret-value", str(raised.exception))
        self.assertNotIn("sensitive-response", str(raised.exception))

    def test_malformed_runs_response_fails_closed_before_post(self):
        outputs = iter([_remote_ref(), json.dumps({"workflow_runs": None})])
        with patch(
            "scripts.dispatch_workflow_governed.run",
            side_effect=lambda *args: next(outputs),
        ) as mocked:
            with self.assertRaisesRegex(RuntimeError, "runs response invalid"):
                dispatch(REQSYS_REPO, WF, "main", "corr-invalid-response", SHA)
        self.assertEqual(mocked.call_count, 2)


if __name__ == "__main__":
    unittest.main()

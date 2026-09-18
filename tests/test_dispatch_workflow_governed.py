from unittest.mock import patch
import json
import pytest
from scripts.dispatch_workflow_governed import dispatch, validate

REPO = "ericson-j-santos/reqsys-v2-enterprise-real"
WF = "planner-teams-notify-dev-acceptance.yml"
WEEKLY_WF = "reqsys-weekly-accomplishment-log.yml"


def test_rejects_repo_outside_allowlist():
    with pytest.raises(ValueError, match="repositório"):
        validate("other/repo", WF, "main")


def test_rejects_workflow_outside_allowlist():
    with pytest.raises(ValueError, match="workflow"):
        validate(REPO, "danger.yml", "main")


def test_accepts_reqsys_weekly_accomplishment_workflow_on_main():
    assert validate(REPO, WEEKLY_WF, "main") == (REPO, WEEKLY_WF, "main")


def test_rejects_ref_outside_allowlist():
    with pytest.raises(ValueError, match="ref"):
        validate(REPO, WF, "prod")


def test_dispatch_confirms_new_run_id():
    before = json.dumps({"workflow_runs": [{"id": 10, "head_branch": "main"}]})
    after = json.dumps({"workflow_runs": [{"id": 11, "head_branch": "main", "html_url": "https://example/run/11", "status": "queued"}, {"id": 10, "head_branch": "main"}]})
    outputs = iter([before, "", after])
    with patch("scripts.dispatch_workflow_governed.run", side_effect=lambda *args: next(outputs)):
        result = dispatch(REPO, WF, "main", "corr-1")
    assert result["result"] == "DISPATCHED"
    assert result["run_id"] == 11
    assert result["correlation_id"] == "corr-1"


def test_dispatch_retries_confirmation_without_repeating_post():
    before = json.dumps({"workflow_runs": [{"id": 10, "head_branch": "main"}]})
    empty = json.dumps({"workflow_runs": [{"id": 10, "head_branch": "main"}]})
    visible = json.dumps({
        "workflow_runs": [
            {"id": 11, "head_branch": "main", "html_url": "https://example/run/11", "status": "queued"},
            {"id": 10, "head_branch": "main"},
        ]
    })
    outputs = iter([before, "", empty, visible])
    calls = []

    def fake_run(*args):
        calls.append(args)
        return next(outputs)

    with patch("scripts.dispatch_workflow_governed.run", side_effect=fake_run), patch(
        "scripts.dispatch_workflow_governed.time.sleep"
    ) as sleep:
        result = dispatch(REPO, WEEKLY_WF, "main", "corr-retry")

    post_calls = [args for args in calls if "--method" in args and "POST" in args]
    assert len(post_calls) == 1
    sleep.assert_called_once_with(1)
    assert result["run_id"] == 11
    assert result["correlation_id"] == "corr-retry"


def test_dispatch_stops_after_bounded_confirmation_attempts():
    before = json.dumps({"workflow_runs": [{"id": 10, "head_branch": "main"}]})
    empty = json.dumps({"workflow_runs": [{"id": 10, "head_branch": "main"}]})
    outputs = iter([before, "", empty, empty, empty])

    with patch("scripts.dispatch_workflow_governed.DISPATCH_CONFIRM_ATTEMPTS", 3), patch(
        "scripts.dispatch_workflow_governed.run", side_effect=lambda *args: next(outputs)
    ), patch("scripts.dispatch_workflow_governed.time.sleep") as sleep:
        with pytest.raises(RuntimeError, match="retentativas limitadas"):
            dispatch(REPO, WEEKLY_WF, "main", "corr-timeout")

    assert [call.args[0] for call in sleep.call_args_list] == [1, 2]

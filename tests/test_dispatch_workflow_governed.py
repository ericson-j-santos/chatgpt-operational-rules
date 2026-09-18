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

from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.publish_branch_governed import publish, validate_branch

HEAD = "1" * 40


def test_rejects_protected_branch():
    with pytest.raises(ValueError):
        validate_branch("main")


def test_rejects_invalid_branch():
    with pytest.raises(ValueError):
        validate_branch("bad branch")


def test_publish_new_branch_and_verify_remote():
    outputs = iter([HEAD, "", "fix/safe", "", "", f"{HEAD}\trefs/heads/fix/safe"])
    with patch("scripts.publish_branch_governed.run", side_effect=lambda *args: next(outputs)) as mocked:
        result = publish(Path("."), "fix/safe", HEAD)
    assert result["result"] == "PUBLISHED"
    assert result["remote_sha"] == HEAD
    assert any(call.args[1] == "push" for call in mocked.call_args_list)


def test_idempotent_when_remote_already_matches():
    outputs = iter([HEAD, "", "fix/safe", f"{HEAD}\trefs/heads/fix/safe"])
    with patch("scripts.publish_branch_governed.run", side_effect=lambda *args: next(outputs)) as mocked:
        result = publish(Path("."), "fix/safe", HEAD)
    assert result["result"] == "ALREADY_PUBLISHED"
    assert not any(call.args[1] == "push" for call in mocked.call_args_list)


def test_refuses_remote_branch_at_different_sha():
    other = "2" * 40
    outputs = iter([HEAD, "", "fix/safe", f"{other}\trefs/heads/fix/safe"])
    with patch("scripts.publish_branch_governed.run", side_effect=lambda *args: next(outputs)):
        with pytest.raises(RuntimeError, match="SHA diferente"):
            publish(Path("."), "fix/safe", HEAD)


def test_refuses_dirty_tree():
    outputs = iter([HEAD, " M file.txt"])
    with patch("scripts.publish_branch_governed.run", side_effect=lambda *args: next(outputs)):
        with pytest.raises(RuntimeError, match="limpo"):
            publish(Path("."), "fix/safe", HEAD)

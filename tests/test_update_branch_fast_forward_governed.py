from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "update_branch_fast_forward_governed.py"
SPEC = importlib.util.spec_from_file_location("update_branch_fast_forward_governed", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def fake_git_factory(remote_values: list[str] | None = None, ancestor: int = 0, dirty: bool = False):
    remote_values = list(remote_values if remote_values is not None else [
        module.EXPECTED_REMOTE_SHA, module.EXPECTED_REMOTE_SHA, module.EXPECTED_HEAD_SHA,
    ])
    calls: list[tuple[str, ...]] = []

    def fake_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args == ("remote", "get-url", "origin"):
            return subprocess.CompletedProcess(args, 0, "https://github.com/ericson-j-santos/desktop-pc24x7-runtime.git\n", "")
        if args == ("branch", "--show-current"):
            return subprocess.CompletedProcess(args, 0, module.EXPECTED_BRANCH + "\n", "")
        if args == ("rev-parse", "HEAD"):
            return subprocess.CompletedProcess(args, 0, module.EXPECTED_HEAD_SHA + "\n", "")
        if args == ("status", "--porcelain"):
            return subprocess.CompletedProcess(args, 0, " M fixture.py\n" if dirty else "", "")
        if args[:3] == ("ls-remote", "--heads", "origin"):
            value = remote_values.pop(0)
            return subprocess.CompletedProcess(args, 0, f"{value}\trefs/heads/{module.EXPECTED_BRANCH}\n", "")
        if args[:2] == ("merge-base", "--is-ancestor"):
            return subprocess.CompletedProcess(args, ancestor, "", "")
        if args[:1] == ("push",):
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)

    return fake_git, calls


class GovernedUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        # Only the test fixture changes: the operational Windows allowlist remains intact.
        root_patch = patch.object(module, "EXPECTED_REPO_ROOT", str(root))
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.base_args = {
            "repo": root,
            "remote": "origin",
            "branch": module.EXPECTED_BRANCH,
            "expected_remote": module.EXPECTED_REMOTE_SHA,
            "expected_head": module.EXPECTED_HEAD_SHA,
        }

    def test_update_uses_plain_fast_forward_push_and_readback(self) -> None:
        fake_git, calls = fake_git_factory()
        with patch.object(module, "git", side_effect=fake_git):
            evidence = module.update(**self.base_args)
        self.assertEqual(evidence["fast_forward"], "true")
        self.assertEqual(evidence["force_push"], "false")
        self.assertEqual(evidence["remote_compare_atomic"], "false")
        self.assertEqual(evidence["post_remote"], module.EXPECTED_HEAD_SHA)
        self.assertEqual(1, sum(call[:1] == ("push",) for call in calls))
        self.assertIn(("push", "origin", f"HEAD:refs/heads/{module.EXPECTED_BRANCH}"), calls)
        self.assertTrue(all("force" not in token for call in calls for token in call))

    def test_update_aborts_if_remote_changes_between_checks(self) -> None:
        fake_git, calls = fake_git_factory([module.EXPECTED_REMOTE_SHA, "4" * 40])
        with patch.object(module, "git", side_effect=fake_git):
            with self.assertRaisesRegex(RuntimeError, "during pre-push"):
                module.update(**self.base_args)
        self.assertFalse(any(call[:1] == ("push",) for call in calls))

    def test_update_rejects_non_ancestor(self) -> None:
        fake_git, calls = fake_git_factory(ancestor=1)
        with patch.object(module, "git", side_effect=fake_git):
            with self.assertRaisesRegex(RuntimeError, "fast-forward"):
                module.update(**self.base_args)
        self.assertFalse(any(call[:1] == ("push",) for call in calls))

    def test_update_is_one_branch_one_sha_pair(self) -> None:
        for field in ("remote", "branch", "expected_remote", "expected_head"):
            with self.subTest(field=field):
                values = dict(self.base_args)
                values[field] = "different"
                with self.assertRaises(RuntimeError):
                    module.validate(**values)

    def test_different_root_is_rejected_before_git(self) -> None:
        values = dict(self.base_args, repo=self.base_args["repo"] / "outside")
        with patch.object(module, "git") as git:
            with self.assertRaisesRegex(RuntimeError, "repository path"):
                module.validate(**values)
        git.assert_not_called()

    def test_dirty_worktree_does_not_push(self) -> None:
        fake_git, calls = fake_git_factory(dirty=True)
        with patch.object(module, "git", side_effect=fake_git):
            with self.assertRaisesRegex(RuntimeError, "clean"):
                module.update(**self.base_args)
        self.assertFalse(any(call[:1] == ("push",) for call in calls))

    def test_replay_of_one_time_operation_does_not_push_again(self) -> None:
        fake_git, calls = fake_git_factory([module.EXPECTED_HEAD_SHA])
        with patch.object(module, "git", side_effect=fake_git):
            with self.assertRaisesRegex(RuntimeError, "before maintenance"):
                module.update(**self.base_args)
        self.assertFalse(any(call[:1] == ("push",) for call in calls))

    def test_incorrect_final_readback_is_not_success(self) -> None:
        fake_git, _ = fake_git_factory([
            module.EXPECTED_REMOTE_SHA, module.EXPECTED_REMOTE_SHA, "4" * 40,
        ])
        with patch.object(module, "git", side_effect=fake_git):
            with self.assertRaisesRegex(RuntimeError, "post-push"):
                module.update(**self.base_args)


if __name__ == "__main__":
    unittest.main()

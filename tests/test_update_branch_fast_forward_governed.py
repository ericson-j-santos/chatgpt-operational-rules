from __future__ import annotations

import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "update_branch_fast_forward_governed.py"
SPEC = importlib.util.spec_from_file_location("update_branch_fast_forward_governed", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


BASE_ARGS = {
    "repo": Path(module.EXPECTED_REPO_ROOT),
    "remote": "origin",
    "branch": module.EXPECTED_BRANCH,
    "expected_remote": module.EXPECTED_REMOTE_SHA,
    "expected_head": module.EXPECTED_HEAD_SHA,
}


def fake_git_factory(remote_values: list[str] | None = None, ancestor: int = 0):
    remote_values = list(remote_values or [module.EXPECTED_REMOTE_SHA, module.EXPECTED_REMOTE_SHA, module.EXPECTED_HEAD_SHA])
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
            return subprocess.CompletedProcess(args, 0, "", "")
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
    def test_update_uses_plain_fast_forward_push_and_readback(self) -> None:
        fake_git, calls = fake_git_factory()
        with patch.object(module, "git", side_effect=fake_git):
            evidence = module.update(**BASE_ARGS)
        self.assertEqual(evidence["fast_forward"], "true")
        self.assertEqual(evidence["force_push"], "false")
        self.assertEqual(evidence["remote_compare_atomic"], "false")
        self.assertIn(("push", "origin", f"HEAD:refs/heads/{module.EXPECTED_BRANCH}"), calls)
        self.assertTrue(all("force" not in token for call in calls for token in call))

    def test_update_aborts_if_remote_changes_between_checks(self) -> None:
        fake_git, _ = fake_git_factory([module.EXPECTED_REMOTE_SHA, "4" * 40])
        with patch.object(module, "git", side_effect=fake_git):
            with self.assertRaisesRegex(RuntimeError, "during pre-push"):
                module.update(**BASE_ARGS)

    def test_update_rejects_non_ancestor(self) -> None:
        fake_git, _ = fake_git_factory(ancestor=1)
        with patch.object(module, "git", side_effect=fake_git):
            with self.assertRaisesRegex(RuntimeError, "fast-forward"):
                module.update(**BASE_ARGS)

    def test_update_is_one_branch_one_sha_pair(self) -> None:
        for field in ("remote", "branch", "expected_remote", "expected_head"):
            with self.subTest(field=field):
                values = dict(BASE_ARGS)
                values[field] = "different"
                with self.assertRaises(RuntimeError):
                    module.validate(**values)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import materialize_clean_repo as m


class MaterializeCleanRepoTests(unittest.TestCase):
    def _git(self, cwd: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
        )
        return completed.stdout.strip()

    def _source_repo(self, root: Path) -> tuple[Path, str]:
        source = root / "source"
        source.mkdir()
        self._git(source, "init", "--quiet")
        self._git(source, "config", "user.email", "fixture@example.invalid")
        self._git(source, "config", "user.name", "Fixture")
        (source / "marker.txt").write_text("materializer-e2e\n", encoding="utf-8")
        self._git(source, "add", "marker.txt")
        self._git(source, "commit", "--quiet", "-m", "fixture")
        return source, self._git(source, "rev-parse", "HEAD")

    def test_validate_sha_requires_full_commit(self) -> None:
        sha = "A" * 40
        self.assertEqual(m.validate_sha(sha), "a" * 40)
        with self.assertRaises(m.MaterializeError):
            m.validate_sha("abc123")

    def test_destination_must_be_new_and_inside_work_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workers"
            root.mkdir()
            target = root / "new-repo"
            _, resolved = m.ensure_destination(target, root)
            self.assertEqual(resolved, target.resolve())
            existing = root / "existing"
            existing.mkdir()
            with self.assertRaises(m.MaterializeError):
                m.ensure_destination(existing, root)
            with self.assertRaises(m.MaterializeError):
                m.ensure_destination(Path(tmp) / "outside", root)

    def test_unlisted_repository_fails_before_creating_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workers"
            root.mkdir()
            target = root / "blocked"
            with self.assertRaisesRegex(m.MaterializeError, "não permitido"):
                m.materialize("other/repo", "1" * 40, target, root, "corr-blocked")
            self.assertFalse(target.exists())

    def test_materialize_exact_commit_and_reject_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, commit = self._source_repo(root)
            workers = root / "workers"
            workers.mkdir()
            target = workers / "clean"
            with mock.patch.dict(m.REPOSITORIES, {"fixture/repo": str(source)}, clear=False):
                result = m.materialize(
                    "fixture/repo", commit, target, workers, "corr-success"
                )
                self.assertEqual(result["result"], "CLEAN_REPO_MATERIALIZED")
                self.assertEqual(result["commit"], commit)
                self.assertEqual(result["correlation_id"], "corr-success")
                self.assertTrue(result["clean"])
                self.assertEqual((target / "marker.txt").read_text(), "materializer-e2e\n")
                self.assertEqual(self._git(target, "status", "--porcelain=v1"), "")
                with self.assertRaisesRegex(m.MaterializeError, "já existe"):
                    m.materialize(
                        "fixture/repo", commit, target, workers, "corr-replay"
                    )

    def test_failure_removes_partial_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workers = Path(tmp) / "workers"
            workers.mkdir()
            target = workers / "partial"
            with mock.patch.dict(
                m.REPOSITORIES, {"fixture/repo": "https://example.invalid/repo.git"}, clear=False
            ), mock.patch.object(m, "run_git", side_effect=m.MaterializeError("forced")):
                with self.assertRaisesRegex(m.MaterializeError, "forced"):
                    m.materialize(
                        "fixture/repo", "2" * 40, target, workers, "corr-failure"
                    )
            self.assertFalse(target.exists())

    def test_run_git_disables_interactive_credentials(self) -> None:
        completed = subprocess.CompletedProcess(["git"], 0, stdout="ok\n", stderr="")
        with mock.patch("subprocess.run", return_value=completed) as run:
            self.assertEqual(m.run_git("git", ["version"]), "ok")
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GCM_INTERACTIVE"], "never")


if __name__ == "__main__":
    unittest.main()
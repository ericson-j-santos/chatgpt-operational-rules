from __future__ import annotations

import sys
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import command_gateway as cg
import session_bootstrap as sb
import session_launcher as sl


class SessionLauncherTests(unittest.TestCase):
    def test_parse_version(self) -> None:
        self.assertEqual(sl.parse_version("1.6.0"), (1, 6, 0))
        with self.assertRaises(cg.GatewayError):
            sl.parse_version("1.6")

    def test_minimum_version_is_enforced(self) -> None:
        self.assertEqual(sl.ensure_min_version({"rules_version": "1.5.2"}), "1.5.2")
        with self.assertRaises(cg.GatewayError) as ctx:
            sl.ensure_min_version({"rules_version": "1.5.1"})
        self.assertEqual(ctx.exception.exit_code, sl.EXIT_SESSION_LAUNCHER)

    def test_generated_session_id_is_valid(self) -> None:
        value = sl.generate_session_id(Path("C:/dev/chatgpt-workers/reqsys-main-canonical"), "ReqSys")
        self.assertLessEqual(len(value), 64)
        self.assertEqual(sb.validate_session_id(value), value)
        self.assertTrue(value.startswith("reqsys-"))

    def test_expected_head_requires_full_sha(self) -> None:
        head = "a" * 40
        self.assertEqual(sl.validate_expected_head(head.upper()), head)
        with self.assertRaises(cg.GatewayError):
            sl.validate_expected_head("abc123")

    def test_transient_session_source_is_exact_and_never_general_allowlist(self) -> None:
        policy = {
            "allowed_roots": ["/safe"],
            "denied_roots": ["/tmp"],
            "denied_segments": [".ssh"],
            "session_source_roots": ["/tmp/actions/repo"],
        }
        self.assertTrue(sl.validate_session_source(Path("/tmp/actions/repo"), policy))
        self.assertFalse(sl.validate_session_source(Path("/safe/repo"), policy))
        with self.assertRaises(cg.GatewayError):
            sl.validate_session_source(Path("/tmp/actions/repo/nested"), policy)

    def test_transient_session_source_rejects_wildcards(self) -> None:
        policy = {
            "allowed_roots": ["/safe"],
            "denied_roots": ["/tmp"],
            "denied_segments": [],
            "session_source_roots": ["/tmp/actions/*"],
        }
        with self.assertRaises(cg.GatewayError):
            sl.validate_session_source(Path("/tmp/actions/repo"), policy)

    def test_sync_ref_requires_remote_branch(self) -> None:
        self.assertEqual(sl.validate_sync_ref("origin/main"), ("origin", "main"))
        self.assertEqual(
            sl.validate_sync_ref("upstream/release/1.6"),
            ("upstream", "release/1.6"),
        )
        for invalid in ("main", "../main", "origin/../main", "origin//main", "-origin/main", "origin/main:evil"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(cg.GatewayError):
                    sl.validate_sync_ref(invalid)



    def test_restricted_fetch_refspec_pins_only_the_requested_branch(self) -> None:
        expected = "a" * 40
        ok = lambda out="": SimpleNamespace(returncode=0, stdout=out, stderr="")
        missing = SimpleNamespace(returncode=128, stdout="", stderr="not found")
        calls = [ok(), missing, ok(), ok(expected + "\n"), ok("https://github.com/example/repo.git\n")]
        with mock.patch.object(sl.cg, "run_capture", side_effect=calls) as captured:
            result = sl._fetch_and_verify_remote(Path("/safe/source"), expected, "origin/main")
        self.assertEqual(result, ("origin", "main", "https://github.com/example/repo.git"))
        commands = [call.args[0] for call in captured.call_args_list]
        self.assertEqual(
            commands[2],
            ["git", "fetch", "--no-tags", "origin", "refs/heads/main:refs/remotes/origin/main"],
        )
        self.assertEqual(len(commands), 5)

    def test_restricted_fetch_missing_branch_fails_closed(self) -> None:
        expected = "a" * 40
        ok = SimpleNamespace(returncode=0, stdout="", stderr="")
        missing = SimpleNamespace(returncode=128, stdout="", stderr="not found")
        with mock.patch.object(sl.cg, "run_capture", side_effect=[ok, missing, missing]) as captured:
            with self.assertRaises(cg.GatewayError) as ctx:
                sl._fetch_and_verify_remote(Path("/safe/source"), expected, "origin/main")
        self.assertEqual(ctx.exception.exit_code, cg.EXIT_STATE_CHANGED)
        self.assertEqual(captured.call_count, 3)

    def test_restricted_fetch_different_sha_still_blocks(self) -> None:
        expected = "a" * 40
        ok = lambda out="": SimpleNamespace(returncode=0, stdout=out, stderr="")
        missing = SimpleNamespace(returncode=128, stdout="", stderr="not found")
        with mock.patch.object(
            sl.cg, "run_capture", side_effect=[ok(), missing, ok(), ok("b" * 40 + "\n")]
        ) as captured:
            with self.assertRaises(cg.GatewayError) as ctx:
                sl._fetch_and_verify_remote(Path("/safe/source"), expected, "origin/main")
        self.assertEqual(ctx.exception.exit_code, cg.EXIT_STATE_CHANGED)
        self.assertEqual(captured.call_count, 4)

    def test_existing_tracking_ref_does_not_fetch_twice(self) -> None:
        expected = "a" * 40
        ok = lambda out="": SimpleNamespace(returncode=0, stdout=out, stderr="")
        with mock.patch.object(
            sl.cg, "run_capture",
            side_effect=[ok(), ok(expected + "\n"), ok("https://github.com/example/repo.git\n")],
        ) as captured:
            self.assertEqual(
                sl._fetch_and_verify_remote(Path("/safe/source"), expected, "origin/main"),
                ("origin", "main", "https://github.com/example/repo.git"),
            )
        self.assertEqual(captured.call_count, 3)

    def test_isolated_local_clone_uses_pinned_commit_without_network_fetch(self) -> None:
        """Live Git fixture: no second fetch, correct SHA, clean reuse."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            worktrees = root / "sessions"
            policy = {
                "worktree_root": str(worktrees),
                "allowed_roots": [str(root)],
                "git_state_timeout_seconds": 30,
            }

            def git(cwd: Path, *args: str) -> str:
                completed = subprocess.run(
                    ["git", *args], cwd=cwd, text=True, capture_output=True,
                    timeout=20, check=True,
                )
                return completed.stdout.strip()

            git(source, "init", "-q")
            git(source, "config", "user.name", "Session Launcher CI")
            git(source, "config", "user.email", "session-launcher@example.invalid")
            (source / "marker.txt").write_text("pinned immutable content\n", encoding="utf-8")
            git(source, "add", "marker.txt")
            git(source, "commit", "-qm", "fixture: pinned source")
            expected = git(source, "rev-parse", "HEAD")
            remote_url = "https://github.invalid/example/repo.git"

            capture = cg.run_capture

            def no_second_network_fetch(args, cwd, timeout=15):
                if list(args[:2]) == ["git", "fetch"]:
                    return subprocess.CompletedProcess(args, 128, "", "network forbidden")
                return capture(args, cwd, timeout=timeout)

            with mock.patch.object(sl.cg, "run_capture", side_effect=no_second_network_fetch):
                isolated = sl._prepare_isolated_base(
                    source, policy, expected, "origin", "main", remote_url, "sha-valid"
                )
                self.assertEqual(isolated, sl._prepare_isolated_base(
                    source, policy, expected, "origin", "main", remote_url, "sha-valid"
                ))
                self.assertEqual(git(isolated, "rev-parse", "HEAD"), expected)
                self.assertEqual(git(isolated, "remote", "get-url", "origin"), remote_url)
                self.assertEqual(
                    (isolated / "marker.txt").read_text(encoding="utf-8"),
                    "pinned immutable content\n",
                )

                # A missing commit must fail closed even though local clone succeeds.
                with self.assertRaises(cg.GatewayError) as blocked:
                    sl._prepare_isolated_base(
                        source, policy, "f" * 40, "origin", "main",
                        remote_url, "sha-absent",
                    )
                self.assertEqual(blocked.exception.exit_code, cg.EXIT_STATE_CHANGED)
                self.assertIn("base isolada sem commit SHA", str(blocked.exception))

    def test_runner_version_preflight_is_fail_closed(self) -> None:
        installed = sl.rvp.RunnerVersion.parse("2.337.0")
        with mock.patch.object(sl.rvp, "detect_runner_version", return_value=installed), mock.patch.object(
            sl.rvp,
            "build_evidence",
            return_value={
                "ok": False,
                "installed_version": "2.337.0",
                "registration_supported": True,
                "runtime_supported_now": False,
            },
        ):
            with self.assertRaises(cg.GatewayError) as ctx:
                sl.enforce_runner_version_preflight()
        self.assertEqual(ctx.exception.exit_code, sl.EXIT_SESSION_LAUNCHER)

    def test_runner_version_preflight_accepts_current_pickup(self) -> None:
        installed = sl.rvp.RunnerVersion.parse("2.337.0")
        evidence = {
            "ok": True,
            "installed_version": "2.337.0",
            "registration_supported": True,
            "runtime_supported_now": True,
            "runtime_support_evidence": "current_job_pickup",
        }
        with mock.patch.object(sl.rvp, "detect_runner_version", return_value=installed), mock.patch.object(
            sl.rvp, "build_evidence", return_value=evidence
        ):
            self.assertEqual(sl.enforce_runner_version_preflight(), evidence)


    def test_github_actions_workspace_source_requires_exact_workspace_and_remote(self) -> None:
        policy = {"denied_segments": [".ssh"]}
        repo = Path("/tmp/actions/work/repo/repo")
        env = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_WORKSPACE": str(repo),
            "GITHUB_SHA": "a" * 40,
            "GITHUB_REPOSITORY": "ericson-j-santos/example-repo",
        }
        remote = SimpleNamespace(
            returncode=0,
            stdout="https://github.com/ericson-j-santos/example-repo.git\n",
            stderr="",
        )
        with mock.patch.dict(sl.os.environ, env, clear=False), mock.patch.object(
            sl.cg, "run_capture", return_value=remote
        ):
            self.assertTrue(sl._github_actions_workspace_source(repo, policy))

    def test_github_actions_workspace_source_rejects_remote_mismatch(self) -> None:
        policy = {"denied_segments": []}
        repo = Path("/tmp/actions/work/repo/repo")
        env = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_WORKSPACE": str(repo),
            "GITHUB_SHA": "b" * 40,
            "GITHUB_REPOSITORY": "ericson-j-santos/example-repo",
        }
        remote = SimpleNamespace(
            returncode=0,
            stdout="https://github.com/ericson-j-santos/other-repo.git\n",
            stderr="",
        )
        with mock.patch.dict(sl.os.environ, env, clear=False), mock.patch.object(
            sl.cg, "run_capture", return_value=remote
        ):
            with self.assertRaises(cg.GatewayError):
                sl._github_actions_workspace_source(repo, policy)

    def test_github_actions_workspace_source_rejects_wrong_path(self) -> None:
        policy = {"denied_segments": []}
        env = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_WORKSPACE": "/tmp/actions/work/repo/repo",
            "GITHUB_SHA": "c" * 40,
            "GITHUB_REPOSITORY": "ericson-j-santos/example-repo",
        }
        with mock.patch.dict(sl.os.environ, env, clear=False):
            self.assertFalse(
                sl._github_actions_workspace_source(
                    Path("/tmp/actions/work/other/other"), policy
                )
            )

    def test_github_actions_workspace_source_rejects_invalid_sha(self) -> None:
        policy = {"denied_segments": []}
        repo = Path("/tmp/actions/work/repo/repo")
        env = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_WORKSPACE": str(repo),
            "GITHUB_SHA": "short",
            "GITHUB_REPOSITORY": "ericson-j-santos/example-repo",
        }
        with mock.patch.dict(sl.os.environ, env, clear=False):
            with self.assertRaises(cg.GatewayError):
                sl._github_actions_workspace_source(repo, policy)


    def test_launcher_bootstraps_sibling_imports_in_isolated_python(self) -> None:
        launcher = SCRIPTS / "session_launcher.py"
        code = (
            "import runpy; "
            f"runpy.run_path({str(launcher)!r}, run_name='session_launcher_import_test')"
        )
        completed = subprocess.run(
            [sys.executable, "-I", "-c", code],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)



    def test_diverged_base_is_isolated_after_remote_sha_validation(self) -> None:
        current = SimpleNamespace(head="1" * 40, status_count=0)
        isolated_state = SimpleNamespace(head="2" * 40, status_count=0)
        policy = {"worktree_root": "/safe", "git_state_timeout_seconds": 15}
        calls = [
            SimpleNamespace(returncode=1, stdout="", stderr=""),
        ]
        with mock.patch.object(sl.cg, "git_state", side_effect=[current, isolated_state]), mock.patch.object(
            sl, "_fetch_and_verify_remote", return_value=("origin", "main", "https://github.com/example/repo.git")
        ), mock.patch.object(sl.cg, "run_capture", side_effect=calls), mock.patch.object(
            sl, "_prepare_isolated_base", return_value=Path("/safe/session-base-test")
        ) as prepare:
            state, path, mode = sl.sync_expected_head(
                Path("/safe/source"), policy, "2" * 40, "origin/main", "test"
            )
        self.assertEqual(state.head, "2" * 40)
        self.assertEqual(path, Path("/safe/session-base-test"))
        self.assertEqual(mode, "isolated_diverged_base")
        prepare.assert_called_once()

    def test_diverged_base_still_blocks_when_remote_sha_mismatches(self) -> None:
        current = SimpleNamespace(head="1" * 40, status_count=0)
        policy = {"worktree_root": "/safe", "git_state_timeout_seconds": 15}
        with mock.patch.object(sl.cg, "git_state", return_value=current), mock.patch.object(
            sl, "_fetch_and_verify_remote", side_effect=cg.GatewayError("remote mismatch", cg.EXIT_STATE_CHANGED)
        ), mock.patch.object(sl, "_prepare_isolated_base") as prepare:
            with self.assertRaises(cg.GatewayError):
                sl.sync_expected_head(Path("/safe/source"), policy, "2" * 40, "origin/main", "test")
        prepare.assert_not_called()



if __name__ == "__main__":
    unittest.main()

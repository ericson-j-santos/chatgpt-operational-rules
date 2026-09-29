from __future__ import annotations

import sys
import subprocess
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



if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
BUNDLE_FILES = (
    "session_launcher.py",
    "command_gateway.py",
    "session_bootstrap.py",
    "session_preflight.py",
    "runner_version_preflight.py",
)


class SessionLauncherDiagnosticCliTests(unittest.TestCase):
    def make_bundle(self, root: Path) -> Path:
        bundle = root / "bundle"
        bundle.mkdir()
        for name in BUNDLE_FILES:
            shutil.copy2(SCRIPTS / name, bundle / name)
        return bundle

    def write_policy(self, root: Path, repo: Path) -> tuple[Path, Path]:
        state_dir = root / "state-must-not-exist"
        policy = root / "policy.json"
        policy.write_text(
            json.dumps(
                {
                    "version": 1,
                    "rules_version": "1.6.11",
                    "state_dir": str(state_dir),
                    "session_source_roots": [str(repo)],
                    "allowed_roots": [str(root / "allowed" / "*")],
                    "denied_roots": ["%USERPROFILE%"],
                }
            ),
            encoding="utf-8",
        )
        return policy, state_dir

    def file_snapshot(self, root: Path) -> dict[str, str]:
        return {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def command(self, bundle: Path, policy: Path, repo: Path) -> list[str]:
        return [
            sys.executable,
            "-B",
            str(bundle / "session_launcher.py"),
            "--policy",
            str(policy),
            "--repo",
            str(repo),
            "--diagnose",
            "--correlation-id",
            "diagnostic-cli-test",
        ]

    def diagnostic_env(self, root: Path) -> dict[str, str]:
        env = dict(os.environ)
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        env.pop("PYTHONPYCACHEPREFIX", None)
        env.update(
            {
                "LOCALAPPDATA": str(root / "token=diagnostic-path-secret"),
                "USERPROFILE": str(root / "profile"),
                "GITHUB_TOKEN": "ghp_must_not_appear_in_diagnostic_output",
                "SECRET_VALUE": "must-not-appear-in-diagnostic-output",
            }
        )
        return env

    def test_real_cli_is_read_only_without_pycache_and_sanitizes_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = self.make_bundle(root)
            repo = root / "checkout"
            policy, state_dir = self.write_policy(root, repo)
            policy_hash = hashlib.sha256(policy.read_bytes()).hexdigest()
            before = self.file_snapshot(bundle)

            completed = subprocess.run(
                self.command(bundle, policy, repo),
                cwd=str(root),
                env=self.diagnostic_env(root),
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["result"], "DIAGNOSTIC_OK")
            self.assertFalse(payload["execution_authorized"])
            self.assertFalse(payload["state_validated"])
            self.assertFalse(payload["launch_invoked"])
            self.assertTrue(payload["session_source_exact_match"])
            self.assertNotIn("target_path", payload)
            self.assertNotIn("snapshot_sha256", payload)
            self.assertEqual(
                set(payload["environment"]),
                {"LOCALAPPDATA", "USERPROFILE"},
            )
            serialized = json.dumps(payload)
            self.assertNotIn("ghp_must_not_appear", serialized)
            self.assertNotIn("must-not-appear-in-diagnostic-output", serialized)
            self.assertNotIn("diagnostic-path-secret", serialized)
            self.assertIn("[REDACTED]", payload["environment"]["LOCALAPPDATA"])
            self.assertEqual(self.file_snapshot(bundle), before)
            self.assertEqual(hashlib.sha256(policy.read_bytes()).hexdigest(), policy_hash)
            self.assertFalse(state_dir.exists())
            self.assertEqual(list(root.rglob("__pycache__")), [])

    def test_real_cli_survives_guards_against_subprocess_events_and_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = self.make_bundle(root)
            repo = root / "checkout"
            policy, state_dir = self.write_policy(root, repo)
            guards = root / "guards"
            guards.mkdir()
            (guards / "sitecustomize.py").write_text(
                """
import builtins
import io
import pathlib
import subprocess

def forbidden(*args, **kwargs):
    raise AssertionError("diagnostic attempted subprocess, event, or write")

subprocess.run = forbidden
pathlib.Path.write_text = forbidden
pathlib.Path.write_bytes = forbidden
pathlib.Path.mkdir = forbidden
pathlib.Path.touch = forbidden

_open = builtins.open
def guarded_open(file, mode="r", *args, **kwargs):
    if any(flag in str(mode) for flag in "wax+"):
        forbidden(file, mode)
    return _open(file, mode, *args, **kwargs)
builtins.open = guarded_open

_io_open = io.open
def guarded_io_open(file, mode="r", *args, **kwargs):
    if any(flag in str(mode) for flag in "wax+"):
        forbidden(file, mode)
    return _io_open(file, mode, *args, **kwargs)
io.open = guarded_io_open
""".lstrip(),
                encoding="utf-8",
            )
            env = self.diagnostic_env(root)
            env["PYTHONPATH"] = str(guards)
            completed = subprocess.run(
                self.command(bundle, policy, repo),
                cwd=str(root),
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout)["result"], "DIAGNOSTIC_OK")
            self.assertFalse(state_dir.exists())
            self.assertEqual(list(root.rglob("__pycache__")), [])

    def test_diagnostic_rejects_launch_arguments_without_semantic_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = self.make_bundle(root)
            repo = root / "checkout"
            policy, state_dir = self.write_policy(root, repo)
            incompatible = (
                ["--session-id", "blocked-session"],
                ["--session-prefix", "blocked-prefix"],
                ["--expected-head", "a" * 40],
                ["--sync-ref", "origin/main"],
                ["--require-runner-version-preflight"],
            )
            for arguments in incompatible:
                with self.subTest(arguments=arguments):
                    command = self.command(bundle, policy, repo) + arguments
                    completed = subprocess.run(
                        command,
                        cwd=str(root),
                        env=self.diagnostic_env(root),
                        text=True,
                        capture_output=True,
                        check=False,
                        timeout=30,
                    )
                    self.assertEqual(completed.returncode, 28)
                    payload = json.loads(completed.stderr)
                    self.assertEqual(payload["result"], "SESSION_LAUNCH_BLOCKED")
                    self.assertEqual(payload["phase"], "diagnostic_validate_arguments")
                    self.assertNotIn("DIAGNOSTIC_OK", completed.stderr)
                    self.assertNotIn("SESSION_LAUNCH_OK", completed.stderr)
                    self.assertFalse(state_dir.exists())

    def test_diagnostic_requires_dash_b_before_sitecustomize(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = self.make_bundle(root)
            repo = root / "checkout"
            policy, state_dir = self.write_policy(root, repo)
            guards = root / "startup"
            guards.mkdir()
            (guards / "startup_probe.py").write_text("VALUE = 1\n", encoding="utf-8")
            (guards / "sitecustomize.py").write_text("import startup_probe\n", encoding="utf-8")
            env = self.diagnostic_env(root)
            env["PYTHONPATH"] = str(guards)
            command = self.command(bundle, policy, repo)
            command.remove("-B")
            completed = subprocess.run(
                command,
                cwd=str(root),
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 28)
            payload = json.loads(completed.stderr)
            self.assertEqual(payload["phase"], "diagnostic_validate_runtime")
            self.assertIn("-B", payload["error"])
            self.assertTrue(list(guards.rglob("*.pyc")))
            self.assertFalse(state_dir.exists())

    def test_real_normal_flow_remains_compatible_in_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = self.make_bundle(root)
            repo = root / "repo"
            workers = root / "workers"
            state_dir = root / "state"
            policy = root / "launch-policy.json"
            git_env = dict(os.environ)
            git_env["GIT_CONFIG_GLOBAL"] = str(root / "gitconfig")
            git_env["GIT_CONFIG_NOSYSTEM"] = "1"
            subprocess.run(["git", "init", str(repo)], env=git_env, check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], env=git_env, check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Diagnostic Test"], env=git_env, check=True)
            (repo / "README.md").write_text("isolated\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "README.md"], env=git_env, check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "baseline"], env=git_env, check=True, capture_output=True)
            policy.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "rules_version": "1.6.11",
                        "state_dir": str(state_dir),
                        "allowed_roots": [str(root)],
                        "denied_roots": [],
                        "denied_segments": [],
                        "allowed_executables": ["git", "python", "python.exe"],
                        "blocked_executables": [],
                        "require_git_repo": True,
                        "risk2_requires_clean_tree": True,
                        "max_timeout_seconds": 120,
                        "git_state_timeout_seconds": 30,
                        "require_session_bootstrap": True,
                        "require_preflight_snapshot": True,
                        "worktree_root": str(workers),
                        "worktree_prefix": "wt-chat-",
                    }
                ),
                encoding="utf-8",
            )
            env = self.diagnostic_env(root)
            env.update(git_env)
            command = [
                sys.executable,
                "-B",
                str(bundle / "session_launcher.py"),
                "--policy", str(policy),
                "--repo", str(repo),
                "--session-id", "diagnostic-normal-flow",
                "--correlation-id", "diagnostic-normal-flow",
            ]
            completed = subprocess.run(
                command,
                cwd=str(root),
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["result"], "SESSION_LAUNCH_OK")
            self.assertTrue(payload["state_validated"])
            self.assertEqual(payload["session_id"], "diagnostic-normal-flow")
            self.assertTrue(Path(payload["target_path"]).is_dir())
            self.assertTrue((state_dir / "sessions" / "diagnostic-normal-flow.json").is_file())
            self.assertTrue((state_dir / "snapshots" / "diagnostic-normal-flow.json").is_file())


if __name__ == "__main__":
    unittest.main()

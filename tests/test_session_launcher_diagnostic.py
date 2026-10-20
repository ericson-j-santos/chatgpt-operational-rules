from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import command_gateway as cg
import session_launcher as sl


class SessionLauncherDiagnosticTests(unittest.TestCase):
    def diagnostic_policy(self, root: Path, source: str) -> Path:
        policy_path = root / "policy.json"
        policy_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "rules_version": "1.6.11",
                    "session_source_roots": [source],
                    "allowed_roots": [str(root / "allowed" / "*")],
                    "denied_roots": ["%USERPROFILE%"],
                }
            ),
            encoding="utf-8",
        )
        return policy_path

    def test_returns_before_launch_git_preflight_events_or_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "source"
            policy_path = self.diagnostic_policy(root, str(repo))
            argv = [
                "session_launcher.py",
                "--policy", str(policy_path),
                "--repo", str(repo),
                "--diagnose",
                "--correlation-id", "diagnostic-test",
            ]
            output = io.StringIO()
            blocked = AssertionError("diagnostic path performed a forbidden operation")
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                sl, "launch", side_effect=blocked
            ), mock.patch.object(
                sl, "_python_started_without_bytecode", return_value=True
            ), mock.patch.object(sl.cg, "run_capture", side_effect=blocked), mock.patch.object(
                sl.sp, "preflight", side_effect=blocked
            ), mock.patch.object(sl.cg, "append_event", side_effect=blocked), mock.patch.object(
                Path, "write_text", side_effect=blocked
            ), mock.patch.object(Path, "write_bytes", side_effect=blocked), mock.patch.object(
                Path, "mkdir", side_effect=blocked
            ), mock.patch.object(sl.os, "open", side_effect=blocked), contextlib.redirect_stdout(output):
                self.assertEqual(sl.main(), 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["result"], "DIAGNOSTIC_OK")
            self.assertEqual(payload["phase"], "diagnostic_complete")
            self.assertTrue(payload["session_source_exact_match"])
            self.assertEqual(payload["correlation_id"], "diagnostic-test")

    def test_error_still_does_not_write_or_run_subprocesses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "missing-policy.json"
            argv = [
                "session_launcher.py",
                "--policy", str(missing),
                "--repo", str(root / "source"),
                "--diagnose",
            ]
            error = io.StringIO()
            blocked = AssertionError("diagnostic error path performed a forbidden operation")
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                sl, "launch", side_effect=blocked
            ), mock.patch.object(
                sl, "_python_started_without_bytecode", return_value=True
            ), mock.patch.object(sl.cg, "run_capture", side_effect=blocked), mock.patch.object(
                sl.sp, "preflight", side_effect=blocked
            ), mock.patch.object(sl.cg, "append_event", side_effect=blocked), mock.patch.object(
                Path, "write_text", side_effect=blocked
            ), mock.patch.object(Path, "write_bytes", side_effect=blocked), mock.patch.object(
                Path, "mkdir", side_effect=blocked
            ), mock.patch.object(sl.os, "open", side_effect=blocked), contextlib.redirect_stderr(error):
                self.assertEqual(sl.main(), sl.EXIT_SESSION_LAUNCHER)
            payload = json.loads(error.getvalue())
            self.assertEqual(payload["result"], "SESSION_LAUNCH_BLOCKED")
            self.assertEqual(payload["phase"], "diagnostic_load_policy")

    def test_limits_environment_and_reports_missing_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy_path = self.diagnostic_policy(root, str(root / "source"))
            with mock.patch.dict(sl.os.environ, {"SECRET_VALUE": "must-not-appear"}, clear=True):
                payload = sl.diagnose(root / "source", policy_path)
            self.assertEqual(
                payload["environment"],
                {
                    "LOCALAPPDATA": None,
                    "USERPROFILE": None,
                    "REQSYS_WORKSPACE_ROOT": None,
                    "REQSYS_GATEWAY_STATE_DIR": None,
                },
            )
            self.assertNotIn("must-not-appear", json.dumps(payload))
            self.assertTrue(payload["policy"]["hash_attests_parsed_bytes"])
            self.assertTrue(all(not item["loaded_bytes_attested"] for item in payload["modules"]))

    def test_sensitive_identifiers_are_rejected_or_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "source"
            policy_path = self.diagnostic_policy(root, str(repo))
            payload = sl.diagnose(
                repo,
                policy_path,
                correlation_id="token=diagnostic-correlation-secret",
            )
            serialized = json.dumps(payload)
            self.assertEqual(payload["correlation_id"], "token=[REDACTED]")
            self.assertNotIn("diagnostic-correlation-secret", serialized)

            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            policy["rules_version"] = "token=diagnostic-rules-secret"
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
            with self.assertRaisesRegex(cg.GatewayError, "rules_version inválida"):
                sl.diagnose(repo, policy_path)

    def test_non_object_policy_roots_fail_with_structured_sanitized_error(self) -> None:
        for value in ([], "policy-root-secret", 7):
            with self.subTest(value_type=type(value).__name__), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                policy_path = root / "policy.json"
                policy_path.write_text(json.dumps(value), encoding="utf-8")
                argv = [
                    "session_launcher.py",
                    "--policy", str(policy_path),
                    "--repo", str(root / "source"),
                    "--diagnose",
                ]
                error = io.StringIO()
                with mock.patch.object(sys, "argv", argv), mock.patch.object(
                    sl, "_python_started_without_bytecode", return_value=True
                ), contextlib.redirect_stderr(error):
                    self.assertEqual(sl.main(), sl.EXIT_SESSION_LAUNCHER)
                serialized = error.getvalue()
                payload = json.loads(serialized)
                self.assertEqual(payload["result"], "SESSION_LAUNCH_BLOCKED")
                self.assertEqual(payload["phase"], "diagnostic_load_policy")
                self.assertIn("policy deve ser objeto", payload["error"])
                self.assertNotIn("policy-root-secret", serialized)

    def test_rejects_non_integer_or_unsupported_policy_versions(self) -> None:
        base = {
            "rules_version": "1.6.11",
            "session_source_roots": [],
            "allowed_roots": [],
            "denied_roots": [],
        }
        for version in (True, False, 1.0, "1", None, 2):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "policy.json"
                path.write_text(json.dumps({**base, "version": version}), encoding="utf-8")
                with self.assertRaisesRegex(cg.GatewayError, "versão de policy"):
                    sl._load_diagnostic_policy(path)

    def test_rejects_invalid_root_lists_and_entries(self) -> None:
        base = {
            "version": 1,
            "rules_version": "1.6.11",
            "session_source_roots": ["C:/source"],
            "allowed_roots": ["C:/allowed/*"],
            "denied_roots": ["C:/denied/*"],
        }
        cases = (
            ("missing", None),
            ("session_source_roots", None),
            ("allowed_roots", {}),
            ("denied_roots", "C:/denied/*"),
            ("session_source_roots", [None]),
            ("allowed_roots", [""]),
            ("denied_roots", ["   "]),
            ("session_source_roots", [1]),
        )
        for key, value in cases:
            with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as tmp:
                policy = dict(base)
                if key == "missing":
                    del policy["session_source_roots"]
                else:
                    policy[key] = value
                path = Path(tmp) / "policy.json"
                path.write_text(json.dumps(policy), encoding="utf-8")
                with self.assertRaises(cg.GatewayError):
                    sl._load_diagnostic_policy(path)

    def test_invalid_platform_uses_launcher_error_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "platform": "unsupported",
                        "rules_version": "1.6.12",
                        "session_source_roots": [],
                        "allowed_roots": [],
                        "denied_roots": [],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(cg.GatewayError) as context:
                sl._load_diagnostic_policy(path)
            self.assertEqual(context.exception.exit_code, sl.EXIT_SESSION_LAUNCHER)

    def test_policy_hash_and_parse_use_the_same_buffer(self) -> None:
        original = json.dumps(
            {
                "version": 1,
                "rules_version": "1.6.11",
                "session_source_roots": [],
                "allowed_roots": [],
                "denied_roots": [],
            }
        ).encode()
        changed = json.dumps(
            {
                "version": 1,
                "rules_version": "9.9.9",
                "session_source_roots": [],
                "allowed_roots": [],
                "denied_roots": [],
            }
        ).encode()
        snapshot = SimpleNamespace(st_dev=1, st_ino=2, st_size=len(original), st_mtime_ns=3)
        path = mock.Mock()
        path.stat.side_effect = [snapshot, snapshot]
        path.read_bytes.return_value = original
        policy, observed = sl._load_diagnostic_policy(path)
        path.read_bytes.return_value = changed
        self.assertEqual(policy["rules_version"], "1.6.11")
        self.assertEqual(observed, hashlib.sha256(original).hexdigest())

    def test_concurrent_file_change_fails_closed(self) -> None:
        before = SimpleNamespace(st_dev=1, st_ino=2, st_size=4, st_mtime_ns=3)
        after = SimpleNamespace(st_dev=1, st_ino=2, st_size=4, st_mtime_ns=4)
        path = mock.Mock()
        path.stat.side_effect = [before, after]
        path.read_bytes.return_value = b"test"
        with self.assertRaisesRegex(cg.GatewayError, "mudou durante o diagnóstico"):
            sl._stable_file_bytes(path, "módulo test")

    def test_exposes_divergent_windows_expansion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "actual" / "repo"
            policy_path = self.diagnostic_policy(root, r"%LOCALAPPDATA%\ReqSys\runner\repo")
            local_app_data = r"C:\Users\service\AppData\Local"

            def expand(value: str) -> str:
                return str(value).replace("%LOCALAPPDATA%", local_app_data).replace(
                    "%USERPROFILE%", r"C:\Users\service"
                )

            with mock.patch.dict(
                sl.os.environ,
                {"LOCALAPPDATA": local_app_data, "USERPROFILE": r"C:\Users\service"},
                clear=True,
            ), mock.patch.object(sl.cg, "expand_path", side_effect=expand):
                payload = sl.diagnose(repo, policy_path)
            item = payload["comparisons"]["session_source_roots"][0]
            self.assertEqual(item["expanded"], local_app_data + r"\ReqSys\runner\repo")
            self.assertFalse(item["exact_match"])
            self.assertFalse(payload["session_source_exact_match"])

    def test_accepts_only_exact_session_source_not_neighbors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            policy_path = self.diagnostic_policy(root, str(source))
            exact = sl.diagnose(source, policy_path)
            child = sl.diagnose(source / "child", policy_path)
            sibling = sl.diagnose(root / "source-copy", policy_path)
            self.assertTrue(exact["session_source_exact_match"])
            self.assertFalse(child["session_source_exact_match"])
            self.assertFalse(sibling["session_source_exact_match"])

    def test_normal_failure_reports_precise_phase(self) -> None:
        argv = ["session_launcher.py", "--policy", "policy.json", "--repo", "repo"]
        error = io.StringIO()

        def fail(**kwargs):
            kwargs["phase_callback"]("validate_session_source")
            raise cg.GatewayError("blocked")

        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            sl, "launch", side_effect=fail
        ), contextlib.redirect_stderr(error):
            self.assertEqual(sl.main(), cg.EXIT_POLICY)
        payload = json.loads(error.getvalue())
        self.assertEqual(payload["phase"], "validate_session_source")


if __name__ == "__main__":
    unittest.main()

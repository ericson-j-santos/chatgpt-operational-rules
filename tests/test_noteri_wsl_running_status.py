"""WSL fixed-running-status gateway: default deny, Noteri-only and no guest launch.

All tests mock wsl.exe. No WSL distribution, PostgreSQL or service is started.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import command_gateway as cg
wsl = cg  # Self-contained installed Gateway, not a sibling runtime dependency.

WSL_EXE = r"C:\Windows\System32\wsl.exe"
EXACT = [WSL_EXE, "--list", "--running", "--quiet"]


def policy(**extra):
    return {
        "version": 1, "platform": "windows",
        "allowed_executables": ["python", "git"],
        "blocked_executables": ["wsl", "cmd", "bash"],
        "denied_names": [".env"], "denied_segments": [".ssh"],
        "deny_inline_code": True,
        "noteri_wsl_running_inventory": True,
        **extra,
    }


class NoteriWslRunningGateTests(unittest.TestCase):
    def setUp(self):
        self.host = mock.patch.object(
            wsl, "wsl_host_context", return_value=("nt", "Noteri"),
        )
        self.sysbinary = mock.patch.object(
            wsl, "wsl_system_path", return_value=WSL_EXE,
        )
        self.host.start()
        self.sysbinary.start()
        self.addCleanup(self.host.stop)
        self.addCleanup(self.sysbinary.stop)

    def test_command_gateway_admits_only_one_fixed_status_command(self):
        cg.validate_command(EXACT, 1, policy())
        self.assertTrue(wsl.wsl_is_command(EXACT))
        self.assertFalse(wsl.wsl_is_command(["python", "-B", "approved.py"]))
        self.assertIn("wsl", policy()["blocked_executables"])

    def test_no_policy_opt_in_or_non_boolean_policy_is_blocked(self):
        for disabled in [False, None, 0, "true", 1]:
            with self.subTest(disabled=disabled):
                config = policy(noteri_wsl_running_inventory=disabled)
                with self.assertRaises(cg.GatewayError):
                    cg.validate_command(EXACT, 1, config)
                if type(disabled) is not bool:
                    with self.assertRaises(cg.GatewayError):
                        cg.validate_policy(config, enforce_platform=False)
        with self.assertRaises(cg.GatewayError):
            cg.validate_command(EXACT, 1, {
                k: v for k, v in policy().items()
                if k != "noteri_wsl_running_inventory"
            })

    def test_wsl_exec_shell_install_shutdown_and_cli_extensions_stay_blocked(self):
        invalid = [
            [WSL_EXE, "--exec", "bash"],
            [WSL_EXE, "--distribution", "Ubuntu", "--exec", "/usr/bin/psql"],
            [WSL_EXE, "--shutdown"],
            [WSL_EXE, "--install"],
            [WSL_EXE, "--list", "--running"],
            [WSL_EXE, "--list", "--running", "--quiet", "--exec"],
            [WSL_EXE, "--LIST", "--running", "--quiet"],
            ["wsl.exe", "--list", "--running", "--quiet"],
            [r"C:\dev\wsl.exe", "--list", "--running", "--quiet"],
        ]
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(cg.GatewayError):
                cg.validate_command(args, 1, policy())
        with self.assertRaises(cg.GatewayError):
            cg.validate_command(EXACT, 2, policy())
        with self.assertRaises(cg.GatewayError):
            cg.validate_command(EXACT, 3, policy())

    def test_wrong_host_or_platform_is_denied_before_execution(self):
        cases = [("nt", "DESKTOP-PDQK954"), ("posix", "Noteri"),
                 ("nt", "Noteri-renamed")]
        for platform, host in cases:
            with self.subTest(host=host), mock.patch.object(
                wsl, "wsl_host_context", return_value=(platform, host),
            ), self.assertRaises(cg.GatewayError):
                cg.validate_command(EXACT, 1, policy())
        with self.assertRaises(cg.GatewayError):
            cg.validate_command(EXACT, 1, policy(platform="posix"))

    def test_utf16le_and_utf8_are_sanitized_without_names(self):
        for raw in [
            "Ubuntu-24.04\r\nPrivateDevDistro\r\n".encode("utf-16le"),
            "Ubuntu-24.04\nPrivateDevDistro\n".encode("utf-8"),
            b"\xff\xfe" + "Ubuntu-24.04\r\n".encode("utf-16le"),
        ]:
            report = wsl.wsl_sanitize_output(0, raw, b"")
            self.assertEqual(report["result"], "NOTERI_WSL_RUNNING_STATUS_READONLY")
            self.assertTrue(report["ubuntu_running_candidate"])
            self.assertEqual(report["ready_for_pr"], False)
            self.assertFalse(report["gateway_posix_verified"])
            self.assertFalse(report["systemd_verified"])
            self.assertFalse(report["linux_command_executed"])
            self.assertFalse(report["distro_names_logged"])
            self.assertNotIn("PrivateDevDistro", json.dumps(report))

    def test_empty_output_is_not_a_linux_launch(self):
        report = wsl.wsl_sanitize_output(0, b"", b"")
        self.assertEqual(report["running_count"], 0)
        self.assertFalse(report["ubuntu_running_candidate"])
        self.assertFalse(report["linux_service_started"])

    def test_bounded_parser_rejects_bad_status_and_injection(self):
        cases = [
            (1, b"Ubuntu", b""),
            (0, b"Ubuntu", b"Warning"),
            (0, b"Ubuntu" * 1000, b""),
            (0, b"Ubuntu\nUbuntu\n", b""),
            (0, b"Ubuntu\n;whoami\n", b""),
            (0, b"Ubuntu\nC:\\secret\n", b""),
            (0, b"\xff\xff", b""),
        ]
        for rc, out, err in cases:
            with self.subTest(rc=rc, bytes=len(out)):
                with self.assertRaises(wsl.WslStatusBlocked):
                    wsl.wsl_sanitize_output(rc, out, err)

    def test_fixed_runner_has_static_argv_short_timeout_and_no_raw_output(self):
        observed = []
        def runner(args, **kwargs):
            observed.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0,
                "Ubuntu-24.04\nPrivateDevDistro\n".encode(), b"")
        for _ in range(2):
            out = wsl.wsl_run_fixed(
                EXACT, 1, policy(), cwd=r"C:\dev\chatgpt-workers\wt-chat-test",
                timeout=60, runner=runner,
            )
            self.assertEqual(out.returncode, 0)
            report = json.loads(out.stdout)
            self.assertEqual(report["running_count"], 2)
            self.assertFalse(report["authority_ready"])
            self.assertEqual(out.stderr, "")
            self.assertNotIn("PrivateDevDistro", out.stdout)
        self.assertEqual(len(observed), 2)
        for args, kw in observed:
            self.assertEqual(args, EXACT)
            self.assertFalse(kw["shell"])
            self.assertFalse(kw["check"])
            self.assertTrue(kw["capture_output"])
            self.assertEqual(kw["timeout"], 12)

    def test_runner_os_error_is_fail_closed_and_non_leaking(self):
        def denied(*_args, **_kw):
            raise OSError("PRIVATE_DRIVE_TOKEN_PATH_MUST_NOT_LEAK")
        out = wsl.wsl_run_fixed(EXACT, 1, policy(), cwd="C:\\dev", timeout=12, runner=denied)
        self.assertEqual(out.returncode, cg.EXIT_COMMAND)
        self.assertNotIn("PRIVATE_DRIVE", out.stdout + out.stderr)
        self.assertEqual(json.loads(out.stdout)["result"],
                         "NOTERI_WSL_RUNNING_STATUS_BLOCKED")

    def test_existing_risk1_gateway_git_sha_and_lock_wrap_fixed_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            state = Path(tmp) / "gateway-state"
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.invalid"],
                           cwd=root, check=True)
            (root / "base.txt").write_text("unchanged\n", encoding="utf-8")
            subprocess.run(["git", "add", "base.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=root,
                           check=True, capture_output=True)
            sha = cg.git_state(root).head
            config = policy(state_dir=str(state),
                            allowed_roots=[cg.norm(root)],
                            require_git_repo=True)
            fixed_output = subprocess.CompletedProcess(
                EXACT, 0, json.dumps({"result": "NOTERI_WSL_RUNNING_STATUS_READONLY",
                                    "ready_for_pr": False}), "",
            )
            with mock.patch.object(wsl, "wsl_run_fixed", return_value=fixed_output) as run:
                result = cg.execute(
                    root, config, EXACT, 1, 15, sha, False, False,
                    "noteri-wsl-status-test",
                )
            self.assertEqual(result, 0)
            self.assertEqual(run.call_count, 1)
            events = (state / "events.jsonl").read_text(encoding="utf-8").splitlines()
            audit = json.loads(events[-1])
            self.assertEqual(audit["result"], "ok")
            self.assertEqual(audit["before"]["head"], audit["after"]["head"])
            self.assertFalse(audit["state_changed"])
            self.assertEqual(audit["gateway_exit_code"], 0)


if __name__ == "__main__":
    unittest.main()

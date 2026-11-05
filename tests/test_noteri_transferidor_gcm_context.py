"""Unit tests for GCM inventory and user-context comparison without raw names."""
import json
import subprocess
import unittest
from unittest.mock import patch
from pathlib import Path
import sys

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import noteri_transferidor_gcm_context as module


class Proc:
    def __init__(self, code=0, stdout="", stderr=""):
        self.returncode = code
        self.stdout = stdout
        self.stderr = stderr


class Tests(unittest.TestCase):
    def execute(self, *, account="alice", console="alice", code=0, stdout="", stderr=""):
        calls = []
        def runner(args, **kwargs):
            calls.append((args, kwargs))
            return Proc(code, stdout, stderr)
        with patch.object(module, "profile_ok", return_value=True):
            result = module.probe(platform="nt", hostname="Noteri",
                                  localappdata="C:/Temp", git="git", environment={
                                      "GH_TOKEN": "hidden_token",
                                      "GIT_TRACE": "1", "GCM_TRACE_SECRETS": "1",
                                  }, identity_reader=lambda: (account, console), runner=runner)
        return result, calls

    def test_empty_inventory(self):
        result, calls = self.execute()
        self.assertEqual(result["result"], "GCM_ACCOUNT_LIST_EMPTY")
        self.assertTrue(result["process_console_user_matches"])
        self.assertTrue(result["identity_comparison_available"])
        self.assertFalse(result["credentials_read"])
        self.assertFalse(result["gcm_login_started"])
        self.assertEqual(len(calls), 1)
        command, kw = calls[0]
        self.assertEqual(command, ["git", "credential-manager", "github", "list", "--url", "https://github.com", "--no-ui"])
        self.assertEqual(kw["env"]["GCM_INTERACTIVE"], "never")
        for key in ("GH_TOKEN", "GIT_TRACE", "GCM_TRACE_SECRETS"):
            self.assertNotIn(key, kw["env"])
        self.assertIs(kw["stdout"], subprocess.PIPE)
        self.assertIs(kw["stderr"], subprocess.PIPE)

    def test_inventory_nonempty_never_outputs_account(self):
        value = "example.com: username=AliceImportantSecret"
        result, _ = self.execute(stdout=value)
        self.assertEqual(result["result"], "GCM_ACCOUNT_LIST_NONEMPTY_UNVERIFIED")
        self.assertTrue(result["gcm_account_list_has_output"])
        self.assertNotIn("AliceImportantSecret", json.dumps(result))

    def test_process_and_console_accounts_different(self):
        result, _ = self.execute(account="ServiceAccountSecret", console="OwnerAccountSecret")
        self.assertFalse(result["process_console_user_matches"])
        self.assertNotIn("Secret", json.dumps(result))

    def test_names_case_insensitive(self):
        result, _ = self.execute(account="USER", console="user")
        self.assertTrue(result["process_console_user_matches"])

    def test_list_failure_does_not_echo_raw_stderr(self):
        raw = "access denied: token=secret_value"
        result, _ = self.execute(code=1, stderr=raw)
        self.assertEqual(result["result"], "GCM_ACCOUNT_LIST_FAILED")
        self.assertNotIn(raw, json.dumps(result))

    def test_identity_unavailable_does_not_claim_match(self):
        with patch.object(module, "profile_ok", return_value=True):
            result = module.probe(platform="nt", hostname="Noteri", localappdata="x",
                 git="git", environment={}, identity_reader=lambda: None,
                 runner=lambda *a, **kw: Proc())
        self.assertFalse(result["identity_comparison_available"])
        self.assertFalse(result["process_console_user_matches"])

    def test_timeout_fail_closed(self):
        def timeout(*a, **kw):
            raise subprocess.TimeoutExpired(a, 15)
        with patch.object(module, "profile_ok", return_value=True):
            result = module.probe(platform="nt", hostname="Noteri", localappdata="x",
                 git="git", environment={}, identity_reader=lambda: ("x", "x"),
                 runner=timeout)
        self.assertEqual(result["result"], "GCM_ACCOUNT_LIST_UNAVAILABLE")

    def test_bad_host_prevents_subprocess(self):
        with patch.object(module, "profile_ok", return_value=False):
            result = module.probe(platform="posix", hostname="other", localappdata=None,
                git="git", environment={}, identity_reader=lambda: None,
                runner=lambda *a, **kw: self.fail("subprocess must not execute"))
        self.assertEqual(result["result"], "BLOCKED_HOST_OR_PROFILE")

    def test_missing_git_prevents_subprocess(self):
        with patch.object(module, "profile_ok", return_value=True):
            result = module.probe(platform="nt", hostname="Noteri", localappdata="x",
                git=None, environment={}, identity_reader=lambda: None,
                runner=lambda *a, **kw: self.fail("subprocess must not execute"))
        self.assertEqual(result["result"], "BLOCKED_GIT_UNAVAILABLE")

    def test_large_inventory_never_echoed(self):
        result, _ = self.execute(stdout="s"*32769)
        self.assertEqual(result["result"], "GCM_ACCOUNT_LIST_INVALID")
        self.assertNotIn("s"*100, json.dumps(result))

    def test_untrusted_identity_types_not_accepted(self):
        with patch.object(module, "profile_ok", return_value=True):
            result = module.probe(platform="nt", hostname="Noteri", localappdata="x",
                git="git", environment={}, identity_reader=lambda: (123, "user"),
                runner=lambda *a, **kw: Proc())
        self.assertFalse(result["identity_comparison_available"])

    def test_replay_is_readonly_and_deterministic(self):
        one, _ = self.execute(stdout="Account: private")
        two, _ = self.execute(stdout="Account: private")
        self.assertEqual(one, two)
        self.assertFalse(one["state_changed"])


if __name__ == "__main__":
    unittest.main()

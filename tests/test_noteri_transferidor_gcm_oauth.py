"""Risk 3 scoped OAuth gateway contract: no raw credentials or unbounded target."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "noteri_transferidor_gcm_oauth.py"
spec = importlib.util.spec_from_file_location("noteri_transferidor_gcm_oauth", MODULE)
assert spec and spec.loader
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)

class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

class TestarOAuthRestrito(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.local = Path(self.tmp.name)
        profile = self.local / s.PROFILE_SUFFIX
        profile.parent.mkdir(parents=True)
        profile.write_text(json.dumps({"host":"Noteri", "profile":"NORMAL", "accepts_new_development":True}))
        self.calls = []

    def go(self, runner, **kwargs):
        values = dict(confirm=s.CONFIRM, preflight_only=False, os_name="nt",
                      hostname="Noteri", localappdata=str(self.local),
                      environment={"GH_TOKEN": "DO_NOT_LEAK", "GIT_CONFIG_COUNT": "1", "PATH":"test"},
                      git="C:/Program Files/Git/cmd/git.exe", runner=runner)
        values.update(kwargs)
        return s.run(**values)

    def test_other_host_denied_without_subprocess(self):
        r = s.run(confirm=s.CONFIRM, preflight_only=False, os_name="nt", hostname="Other",
                  localappdata=str(self.local),environment={},git="git",runner=lambda *a,**k: self.fail("ran"))
        self.assertEqual(r["status"], "BLOCKED_HOST_OR_PROFILE")

    def test_nonwindows_denied(self):
        r = s.run(confirm=s.CONFIRM, preflight_only=False,os_name="posix",hostname="Noteri",
                  localappdata=str(self.local),environment={},git="git",runner=lambda *a,**k:self.fail("ran"))
        self.assertEqual(r["status"], "BLOCKED_HOST_OR_PROFILE")

    def test_missing_or_estudo_profile_denied(self):
        profile = self.local / s.PROFILE_SUFFIX
        profile.write_text(json.dumps({"host":"Noteri", "profile":"ESTUDO", "accepts_new_development":False}))
        self.assertEqual(self.go(lambda *a,**k:self.fail("ran"))["status"], "BLOCKED_HOST_OR_PROFILE")
        profile.unlink()
        self.assertEqual(self.go(lambda *a,**k:self.fail("ran"))["status"], "BLOCKED_HOST_OR_PROFILE")

    def test_already_ready_idempotent(self):
        def runner(args, **kw):
            self.calls.append((args,kw))
            return Result(stdout=s.TARGET_SHA+"\t"+s.TARGET_REF+"\n")
        r=self.go(runner)
        self.assertEqual(r["status"],"SOURCE_AUTH_READY")
        self.assertFalse(r["oauth_browser_requested"])
        self.assertEqual(len(self.calls),1)

    def test_login_and_readback_with_no_secrets_in_arguments_or_environment(self):
        def runner(args,**kw):
            self.calls.append((args,kw))
            if len(self.calls)==1:return Result(returncode=128)
            if len(self.calls)==2:return Result()
            return Result(stdout=s.TARGET_SHA+"\t"+s.TARGET_REF+"\n")
        r=self.go(runner)
        self.assertEqual(r["status"],"SOURCE_AUTH_READY")
        self.assertTrue(r["oauth_browser_requested"])
        self.assertEqual(len(self.calls),3)
        self.assertEqual(self.calls[1][0][1:], ["credential-manager","github","login","--browser","--url","https://github.com"])
        self.assertEqual(self.calls[1][1]["stdout"], subprocess.DEVNULL)
        self.assertEqual(self.calls[1][1]["stderr"], subprocess.PIPE)
        self.assertEqual(self.calls[1][1]["stdout"], subprocess.DEVNULL)
        self.assertNotIn("GH_TOKEN",self.calls[1][1]["env"])
        self.assertNotIn("GIT_CONFIG_COUNT",self.calls[1][1]["env"])
        self.assertNotIn("DO_NOT_LEAK",str(r))

    def test_login_cannot_be_started_without_confirm(self):
        self.assertEqual(self.go(lambda *a,**k:Result(returncode=128), confirm="wrong")["status"], "BLOCKED_CONFIRMATION")

    def test_preflight_only_never_launches_browser(self):
        r=s.run(confirm="",preflight_only=True,os_name="nt",hostname="Noteri",
                localappdata=str(self.local),environment={},git="git",runner=lambda *a,**k:Result(returncode=128))
        self.assertEqual(r["status"],"OAUTH_HUMAN_CONSENT_REQUIRED")

    def test_head_mismatch_fails_closed_after_login(self):
        count=0
        def runner(args,**kw):
            nonlocal count
            count+=1
            if count==1:return Result(returncode=128)
            if count==2:return Result(returncode=0)
            return Result(stdout="a"*40+"\t"+s.TARGET_REF+"\n")
        self.assertEqual(self.go(runner)["status"],"BLOCKED_PRIVATE_REPO_READBACK")

    def test_login_failure_and_timeout_no_false_success(self):
        n=0
        def failed(args,**kw):
            nonlocal n
            n+=1
            return Result(returncode=128)
        self.assertEqual(self.go(failed)["status"],"OAUTH_NOT_COMPLETED")
        def hanging(args,**kw):
            if "login" in args:raise subprocess.TimeoutExpired(args,120)
            return Result(returncode=128)
        self.assertEqual(self.go(hanging)["status"],"OAUTH_BROWSER_UNAVAILABLE")

    def test_classification_is_bounded_and_never_reveals_provider_text(self):
        cases = [
            ("Unrecognized option --browser", "OAUTH_GCM_ARGUMENT_UNSUPPORTED"),
            ("Unable to open browser at https://127.0.0.1/?code=SENSITIVE", "OAUTH_BROWSER_UNAVAILABLE"),
            ("User cancelled consent: code ABCD", "OAUTH_CONSENT_CANCELLED"),
            ("Proxy authentication required password=SECRET", "OAUTH_NETWORK_OR_TLS_BLOCKED"),
            ("Access is denied: token SECRET", "OAUTH_LOCAL_CREDENTIAL_STORE_BLOCKED"),
            ("Unexpected failure: callback https://example/?token=SECRET", "OAUTH_NOT_COMPLETED"),
        ]
        for raw, label in cases:
            with self.subTest(label=label):
                result = s.classify_gcm_stderr(raw)
                self.assertEqual(result, label)
                self.assertNotIn("SECRET", result)
                self.assertNotIn("SENSITIVE", result)
        self.assertEqual(s.classify_gcm_stderr(None), "OAUTH_NOT_COMPLETED")

    def test_login_error_is_sanitized_and_not_replayed(self):
        calls = []
        raw = "Unable to open browser; authorization: secret-token"
        def runner(args, **kw):
            calls.append((args, kw))
            if len(calls) == 1:
                return Result(returncode=128)
            return Result(returncode=1, stderr=raw)
        result = self.go(runner)
        self.assertEqual(result["status"], "OAUTH_BROWSER_UNAVAILABLE")
        self.assertTrue(result["oauth_browser_requested"])
        self.assertFalse(result["source_head_verified"])
        self.assertEqual(len(calls), 2)
        self.assertNotIn(raw, json.dumps(result))
        self.assertEqual(calls[1][1]["stderr"], subprocess.PIPE)
        self.assertEqual(calls[1][1]["stdout"], subprocess.DEVNULL)
        self.assertNotIn("GH_TOKEN", calls[1][1]["env"])


    def test_exit_codes_preserve_fail_closed_and_are_unique(self):
        expected = {
            "OAUTH_GCM_ARGUMENT_UNSUPPORTED": 31,
            "OAUTH_BROWSER_UNAVAILABLE": 32,
            "OAUTH_CONSENT_CANCELLED": 33,
            "OAUTH_NETWORK_OR_TLS_BLOCKED": 34,
            "OAUTH_LOCAL_CREDENTIAL_STORE_BLOCKED": 35,
            "OAUTH_NOT_COMPLETED": 36,
            "BLOCKED_PRIVATE_REPO_READBACK": 37,
        }
        self.assertEqual(s.GCM_FAILURE_EXIT_CODES, expected)
        self.assertEqual(len(set(expected.values())), len(expected))
        for status, code in expected.items():
            with self.subTest(status=status):
                self.assertEqual(s.exit_for_status(status, preflight_only=False), code)
                self.assertGreater(code, 0)
        self.assertEqual(s.exit_for_status("SOURCE_AUTH_READY", preflight_only=False), 0)
        self.assertEqual(s.exit_for_status("OAUTH_HUMAN_CONSENT_REQUIRED", preflight_only=True), 0)
        self.assertEqual(s.exit_for_status("OAUTH_HUMAN_CONSENT_REQUIRED", preflight_only=False), 28)
        self.assertEqual(s.exit_for_status("BLOCKED_HOST_OR_PROFILE", preflight_only=False), 28)


if __name__=="__main__":unittest.main()

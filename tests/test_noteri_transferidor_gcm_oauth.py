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
    def __init__(self, returncode=0, stdout=""):
        self.returncode, self.stdout = returncode, stdout

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
        self.assertEqual(self.calls[1][1]["stderr"], subprocess.DEVNULL)
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

if __name__=="__main__":unittest.main()

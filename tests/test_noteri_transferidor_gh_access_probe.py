"""Regressões positivas/negativas da capacidade somente leitura do GH CLI."""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("noteri_transferidor_gh_access_probe", SCRIPTS / "noteri_transferidor_gh_access_probe.py")
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class GHAccessProbeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.binary = self.root / "gh.exe"
        self.binary.write_bytes(b"x" * 2048)
        profile = self.root / "ReqSys" / "TodoGlobal24x7" / "host-profile.json"
        profile.parent.mkdir(parents=True)
        profile.write_text(json.dumps({"host":"Noteri", "profile":"NORMAL", "accepts_new_development":True}), encoding="utf-8")
        self.calls = []

    def runner(self, argv, **kw):
        self.calls.append((argv,kw))
        return subprocess.CompletedProcess(argv, 0, stdout=(module.EXPECTED_SHA + "\n").encode("ascii"), stderr=None)

    def invoke(self, **kwargs):
        args=dict(os_name="nt", hostname="Noteri", localappdata=str(self.root),
                  expected_cli=self.binary, observed_cli=str(self.binary),
                  environment={"GH_TOKEN":"never-expose", "GIT_TRACE":"1", "GH_CONFIG_DIR":"untrusted"}, runner=self.runner)
        args.update(kwargs)
        return module.probe(**args)

    def test_exact_sha_and_safe_cli_no_credential_leak(self):
        r=self.invoke()
        self.assertEqual(r["result"],"PRIVATE_ACCESS_VERIFIED")
        self.assertTrue(r["authenticated_access_verified"])
        argv,opts=self.calls[0]
        self.assertEqual(argv[1:],["api","--hostname","github.com","--method","GET",module.API_RESOURCE,"--jq",".object.sha"])
        self.assertIs(opts["stdin"],subprocess.DEVNULL)
        self.assertIs(opts["stderr"],subprocess.DEVNULL)
        self.assertFalse(opts["shell"])
        self.assertEqual(opts["timeout"],25)
        self.assertNotIn("GH_TOKEN",opts["env"])
        self.assertNotIn("GH_CONFIG_DIR",opts["env"])
        self.assertNotIn("GIT_TRACE",opts["env"])
        self.assertEqual(opts["env"]["GH_PROMPT_DISABLED"],"1")
        self.assertNotIn("never-expose",json.dumps(r))

    def test_replay_idempotent(self):
        first=self.invoke();other=self.invoke()
        self.assertEqual(first,other)
        self.assertFalse(first["state_changed"])
        self.assertEqual(len(self.calls),2)

    def test_wrong_host_rejects_before_cli(self):
        self.assertEqual(self.invoke(hostname="Desktop")["result"],"BLOCKED_HOST_OR_PROFILE")
        self.assertFalse(self.calls)

    def test_nonwindows_rejects_before_cli(self):
        self.assertEqual(self.invoke(os_name="posix")["result"],"BLOCKED_HOST_OR_PROFILE")
        self.assertFalse(self.calls)

    def test_estudo_profile_rejects_before_cli(self):
        p=self.root/"ReqSys"/"TodoGlobal24x7"/"host-profile.json"
        p.write_text(json.dumps({"host":"Noteri","profile":"ESTUDO","accepts_new_development":False}),encoding="utf-8")
        self.assertEqual(self.invoke()["result"],"BLOCKED_HOST_OR_PROFILE")
        self.assertFalse(self.calls)

    def test_missing_gh_rejects_without_invoking(self):
        self.assertEqual(self.invoke(observed_cli=None)["result"],"GH_CLI_UNAVAILABLE")
        self.assertEqual(self.invoke(observed_cli=str(self.root/'impostor.exe'))["result"],"GH_CLI_ORIGIN_UNVERIFIED")
        self.assertFalse(self.calls)

    def test_auth_missing_never_leaks_stderr(self):
        def failure(argv,**kw):
            return subprocess.CompletedProcess(argv,4,stdout=b"",stderr=b"token-would-leak")
        r=self.invoke(runner=failure)
        self.assertEqual(r["result"],"GH_AUTH_OR_PERMISSION_MISSING")
        self.assertNotIn("token",json.dumps(r))

    def test_wrong_sha_fails_closed(self):
        def wrong(argv,**kw):
            return subprocess.CompletedProcess(argv,0,stdout=("f"*40+"\n").encode())
        r=self.invoke(runner=wrong)
        self.assertEqual(r["result"],"BLOCKED_HEAD_MISMATCH")
        self.assertFalse(r["authenticated_access_verified"])

    def test_malformed_response_fails_closed(self):
        for value in (b"",b"error",b"f"*41,b"f"*40+b"\ngarbage",b"\xff"):
            with self.subTest(value=value):
                def malformed(argv,**kw):
                    return subprocess.CompletedProcess(argv,0,stdout=value)
                self.assertEqual(self.invoke(runner=malformed)["result"],"GH_REF_RESPONSE_INVALID")

    def test_timeout_fails_closed(self):
        def timeout(argv,**kw):
            raise subprocess.TimeoutExpired(argv,25)
        r=self.invoke(runner=timeout)
        self.assertEqual(r["result"],"GH_AUTH_PROBE_FAILED")


if __name__=="__main__":unittest.main()

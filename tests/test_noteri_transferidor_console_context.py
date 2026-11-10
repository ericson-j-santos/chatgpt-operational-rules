"""Contract tests for read-only Windows console/session diagnosis."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import noteri_transferidor_console_context as module


class TestNoteriConsoleContext(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.localappdata = Path(self.tmp.name)
        profile = self.localappdata / "ReqSys" / "TodoGlobal24x7" / "host-profile.json"
        profile.parent.mkdir(parents=True)
        profile.write_text(json.dumps({
            "host": "Noteri", "profile": "NORMAL", "accepts_new_development": True,
        }), encoding="utf-8")
        self.calls = 0

    def invoke(self, *, observed=(2, 0), platform="nt", host="Noteri", reader=None):
        def fake_reader():
            self.calls += 1
            return observed
        return module.probe(
            platform=platform, host=host, localappdata=str(self.localappdata),
            reader=reader or fake_reader,
        )

    def test_service_session_zero_is_distinct_from_console(self):
        result = self.invoke(observed=(2, 0))
        self.assertEqual(result["result"], "PROCESS_SESSION_ZERO")
        self.assertFalse(result["same_active_console_session"])
        self.assertFalse(result["interactive_consent_proven"])
        self.assertFalse(result["state_changed"])

    def test_console_candidate_does_not_claim_browser_or_consent(self):
        result = self.invoke(observed=(2, 2))
        self.assertEqual(result["result"], "ACTIVE_CONSOLE_CONTEXT_CANDIDATE")
        self.assertTrue(result["same_active_console_session"])
        self.assertFalse(result["interactive_consent_proven"])
        self.assertFalse(result["browser_launched"])

    def test_mismatch_not_promoted_to_success(self):
        self.assertEqual(self.invoke(observed=(3, 2))["result"], "PROCESS_NOT_IN_ACTIVE_CONSOLE")

    def test_console_zero_never_ready(self):
        self.assertEqual(self.invoke(observed=(0, 3))["result"], "ACTIVE_CONSOLE_SESSION_ZERO")

    def test_unknown_active_session_fails_closed(self):
        self.assertEqual(self.invoke(observed=(0xFFFFFFFF, 3))["result"], "SESSION_QUERY_UNAVAILABLE")

    def test_incomplete_or_invalid_api_result_fails(self):
        for value in (None, (), (2,), (2, "2"), (2, -1), (2, True)):
            with self.subTest(value=value):
                self.assertEqual(self.invoke(observed=value)["result"], "SESSION_QUERY_UNAVAILABLE")

    def test_exception_from_kernel_api_fails_closed(self):
        def bad():
            raise OSError("sensitive context must not leak")
        result = self.invoke(reader=bad)
        self.assertEqual(result["result"], "SESSION_QUERY_UNAVAILABLE")
        self.assertNotIn("sensitive", json.dumps(result))

    def test_wrong_host_never_calls_os_api(self):
        self.assertEqual(self.invoke(host="Other")["result"], "BLOCKED_HOST_OR_PROFILE")
        self.assertEqual(self.calls, 0)

    def test_non_windows_never_calls_os_api(self):
        self.assertEqual(self.invoke(platform="posix")["result"], "BLOCKED_HOST_OR_PROFILE")
        self.assertEqual(self.calls, 0)

    def test_estudo_profile_blocked(self):
        profile = self.localappdata / "ReqSys" / "TodoGlobal24x7" / "host-profile.json"
        profile.write_text(json.dumps({"host": "Noteri", "profile": "ESTUDO", "accepts_new_development": False}), encoding="utf-8")
        self.assertEqual(self.invoke()["result"], "BLOCKED_HOST_OR_PROFILE")
        self.assertEqual(self.calls, 0)

    def test_replay_no_mutation_and_same_result(self):
        one = self.invoke(observed=(2, 0))
        two = self.invoke(observed=(2, 0))
        self.assertEqual(one, two)
        self.assertEqual(self.calls, 2)
        self.assertFalse(one["state_changed"])

    def test_no_kernel_api_called_if_profile_missing(self):
        self.assertEqual(module.probe(platform="nt", host="Noteri", localappdata=None,
                                      reader=lambda: self.fail("API unexpectedly called"))["result"],
                         "BLOCKED_HOST_OR_PROFILE")


if __name__ == "__main__":
    unittest.main()

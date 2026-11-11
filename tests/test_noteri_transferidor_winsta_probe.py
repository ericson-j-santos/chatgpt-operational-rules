"""Regressões herméticas de diagnóstico do atributo WSF_VISIBLE do Windows."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import noteri_transferidor_winsta_probe as module


class TestNoteriWindowStation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.localappdata = Path(self.temp.name)
        p = self.localappdata / "ReqSys" / "TodoGlobal24x7" / "host-profile.json"
        p.parent.mkdir(parents=True)
        self.profile = p
        p.write_text(json.dumps({"host": "Noteri", "profile": "NORMAL",
                                 "accepts_new_development": True}), encoding="utf-8")
        self.session_calls = 0
        self.station_calls = 0

    def run_probe(self, sessions=(2, 2), visible=True, host="Noteri", platform="nt"):
        def read_session():
            self.session_calls += 1
            return sessions
        def read_station():
            self.station_calls += 1
            return visible
        return module.probe(platform=platform, host=host, localappdata=str(self.localappdata),
                            sessions_reader=read_session, station_reader=read_station)

    def test_visible_station_is_only_candidate_not_consent(self):
        x = self.run_probe()
        self.assertEqual(x["result"], "VISIBLE_WINDOW_STATION_CANDIDATE")
        self.assertTrue(x["same_active_console_session"])
        self.assertTrue(x["visible_window_station"])
        self.assertFalse(x["interactive_desktop_proven"])
        self.assertFalse(x["browser_launched"])
        self.assertFalse(x["secret_value_exposed"])
        self.assertFalse(x["state_changed"])

    def test_nonvisible_station_distinct_from_console_session(self):
        x = self.run_probe(visible=False)
        self.assertEqual(x["result"], "WINDOW_STATION_NOT_VISIBLE")
        self.assertTrue(x["same_active_console_session"])
        self.assertFalse(x["visible_window_station"])

    def test_other_session_never_queries_window_station(self):
        self.assertEqual(self.run_probe(sessions=(2, 3))["result"], "PROCESS_NOT_IN_ACTIVE_CONSOLE")
        self.assertEqual(self.station_calls, 0)

    def test_invalid_sessions_fail_closed(self):
        for x in [None, (), (1,), (0, 2), (2, 0), (2, -1), (2, "2"), (2, True), (0xFFFFFFFF, 2)]:
            with self.subTest(x=x):
                self.assertEqual(self.run_probe(sessions=x)["result"], "SESSION_QUERY_UNAVAILABLE")
        self.assertEqual(self.station_calls, 0)

    def test_invalid_window_station_results_fail_closed(self):
        for x in (None, 1, "visible", [], {}):
            with self.subTest(x=x):
                self.assertEqual(self.run_probe(visible=x)["result"], "WINDOW_STATION_QUERY_UNAVAILABLE")

    def test_wrong_host_and_nonwindows_no_native_calls(self):
        self.assertEqual(self.run_probe(host="Other")["result"], "BLOCKED_HOST_OR_PROFILE")
        self.assertEqual(self.run_probe(platform="posix")["result"], "BLOCKED_HOST_OR_PROFILE")
        self.assertEqual(self.session_calls, 0)
        self.assertEqual(self.station_calls, 0)

    def test_estudo_profile_no_native_calls(self):
        self.profile.write_text(json.dumps({"host": "Noteri", "profile": "ESTUDO",
                                            "accepts_new_development": False}), encoding="utf-8")
        self.assertEqual(self.run_probe()["result"], "BLOCKED_HOST_OR_PROFILE")
        self.assertEqual(self.session_calls, 0)

    def test_missing_profile_no_native_calls(self):
        self.profile.unlink()
        self.assertEqual(self.run_probe()["result"], "BLOCKED_HOST_OR_PROFILE")
        self.assertEqual(self.session_calls, 0)

    def test_session_exception_sanitized(self):
        def bad():
            raise OSError("private path or user")
        x = module.probe(platform="nt", host="Noteri", localappdata=str(self.localappdata),
                         sessions_reader=bad, station_reader=lambda: self.fail("should not run"))
        self.assertEqual(x["result"], "SESSION_QUERY_UNAVAILABLE")
        self.assertNotIn("private path", json.dumps(x))

    def test_station_exception_sanitized(self):
        def bad():
            raise OSError("private path or user")
        x = module.probe(platform="nt", host="Noteri", localappdata=str(self.localappdata),
                         sessions_reader=lambda: (2, 2), station_reader=bad)
        self.assertEqual(x["result"], "WINDOW_STATION_QUERY_UNAVAILABLE")
        self.assertNotIn("private path", json.dumps(x))

    def test_replay_idempotent_without_mutation(self):
        first, second = self.run_probe(), self.run_probe()
        self.assertEqual(first, second)
        self.assertEqual(self.session_calls, 2)
        self.assertEqual(self.station_calls, 2)
        self.assertFalse(first["state_changed"])

    def test_nonwindows_native_call_is_blocked_without_profile(self):
        r = module.probe(platform="posix", host="Noteri", localappdata=None,
                         sessions_reader=lambda: self.fail("native call"),
                         station_reader=lambda: self.fail("native call"))
        self.assertEqual(r["result"], "BLOCKED_HOST_OR_PROFILE")


if __name__ == "__main__":
    unittest.main()

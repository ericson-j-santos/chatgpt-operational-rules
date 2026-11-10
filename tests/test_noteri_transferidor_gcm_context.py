"""Negative-first contracts: never inspect Windows accounts or credentials."""
import inspect
import json
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import noteri_transferidor_gcm_context as module


class Tests(unittest.TestCase):
    def invoke(self, pair=(2, 2), persistence="LOCAL_MACHINE"):
        with patch.object(module, "profile_ok", return_value=True):
            return module.probe(platform="nt", hostname="Noteri", localappdata="C:/Temp",
                                session_reader=lambda: pair,
                                persistence_reader=lambda: persistence)

    def test_positive_only_session_capability_candidate(self):
        for label in ("SESSION", "LOCAL_MACHINE", "ENTERPRISE"):
            with self.subTest(label=label):
                result = self.invoke(persistence=label)
                self.assertEqual(result["result"], "CREDENTIAL_PERSISTENCE_CANDIDATE")
                self.assertTrue(result["credential_store_candidate"])
                self.assertTrue(result["same_active_console_session"])
                self.assertFalse(result["source_head_verified"])
                self.assertFalse(result["oauth_browser_requested"])
                self.assertFalse(result["credentials_read"])
                self.assertFalse(result["state_changed"])

    def test_no_persistence_blocks_candidate(self):
        result = self.invoke(persistence="NONE")
        self.assertEqual(result["result"], "CREDENTIAL_PERSISTENCE_NONE")
        self.assertFalse(result["credential_store_candidate"])

    def test_unknown_persistence_fails_closed(self):
        for value in ("UNAVAILABLE", "SENSITIVE_NAME", None, 3, True):
            with self.subTest(value=value):
                result = self.invoke(persistence=value)
                self.assertEqual(result["result"], "CREDENTIAL_PERSISTENCE_UNKNOWN")
                self.assertFalse(result["credential_store_candidate"])
                self.assertNotIn("SENSITIVE_NAME", json.dumps(result))

    def test_no_console_rejects_without_persistence_query(self):
        for pair in ((0, 0), (0, 2), (2, 0), (3, 2)):
            with self.subTest(pair=pair), patch.object(module, "profile_ok", return_value=True):
                result = module.probe(platform="nt", hostname="Noteri", localappdata="x",
                    session_reader=lambda: pair,
                    persistence_reader=lambda: self.fail("must not query"))
                self.assertEqual(result["result"], "PROCESS_NOT_IN_ACTIVE_CONSOLE")
                self.assertEqual(result["credential_persistence"], "NOT_CHECKED")

    def test_invalid_console_result_fails_closed(self):
        for pair in (None, (), (2,), (2, "2"), (2, True), (0xFFFFFFFF, 3)):
            with self.subTest(pair=pair), patch.object(module, "profile_ok", return_value=True):
                result = module.probe(platform="nt", hostname="Noteri", localappdata="x",
                    session_reader=lambda: pair,
                    persistence_reader=lambda: self.fail("must not query"))
                self.assertEqual(result["result"], "SESSION_QUERY_UNAVAILABLE")

    def test_wrong_host_or_profile_never_queries_native_apis(self):
        with patch.object(module, "profile_ok", return_value=False):
            result = module.probe(platform="posix", hostname="other", localappdata=None,
                 session_reader=lambda: self.fail("must not read session"),
                 persistence_reader=lambda: self.fail("must not query"))
        self.assertEqual(result["result"], "BLOCKED_HOST_OR_PROFILE")

    def test_persistence_reader_errors_do_not_leak(self):
        def bad():
            raise OSError("SECRET must not leak")
        with patch.object(module, "profile_ok", return_value=True):
            result = module.probe(platform="nt", hostname="Noteri", localappdata="x",
                session_reader=lambda: (2, 2), persistence_reader=bad)
        self.assertEqual(result["result"], "CREDENTIAL_PERSISTENCE_UNKNOWN")
        self.assertNotIn("SECRET", json.dumps(result))

    def test_replay_no_side_effects(self):
        a = self.invoke()
        b = self.invoke()
        self.assertEqual(a, b)
        self.assertFalse(a["state_changed"])

    def test_wincred_native_api_fake_positive(self):
        for value, label in module.PERSISTENCE.items():
            class CallableApi:
                def __call__(self, count, levels):
                    self.expected = count
                    levels[1] = value
                    return True
            class Dll: pass
            api = Dll()
            api.CredGetSessionTypes = CallableApi()
            self.assertEqual(module.credential_persistence(platform="nt",
                dll_loader=lambda *a, **k: api), label)
            self.assertEqual(api.CredGetSessionTypes.expected, module.CRED_TYPE_MAXIMUM)

    def test_wincred_native_api_failure(self):
        class Api:
            def __call__(self, *args): return False
        class Dll: pass
        dll = Dll();dll.CredGetSessionTypes = Api()
        self.assertEqual(module.credential_persistence(platform="nt", dll_loader=lambda *a,**k: dll), "UNAVAILABLE")
        self.assertEqual(module.credential_persistence(platform="posix",
            dll_loader=lambda *a,**k: self.fail("should not load native library")), "UNAVAILABLE")

    def test_no_identity_or_credential_enumeration_in_source(self):
        source = inspect.getsource(module)
        for forbidden in ("GetUserName", "WTSUserName", "WTSQuerySessionInformation",
                          "credential-manager", "CredRead", "CredEnumerate", "GCM_TRACE"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_receipt_has_no_identity_fields_or_values(self):
        result = self.invoke()
        for forbidden in ("username", "user_name", "account", "sid", "token", "credential_value"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, json.dumps(result).lower())

if __name__ == "__main__":
    unittest.main()

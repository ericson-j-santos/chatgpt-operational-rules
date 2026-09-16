from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import host_health_check as hhc


class HostHealthCheckTests(unittest.TestCase):
    def test_collect_exposes_only_boolean_contract(self) -> None:
        with mock.patch.object(hhc, "gateway_process_active", return_value=True), \
             mock.patch.object(hhc, "credential_present", return_value=True):
            result = hhc.collect()
        self.assertEqual(result, {"gateway_process_active": True, "credential_present": True})
        self.assertTrue(all(type(value) is bool for value in result.values()))

    def test_output_contract_contains_no_sensitive_fields(self) -> None:
        result = {"gateway_process_active": True, "credential_present": True}
        rendered = json.dumps(result).casefold()
        for forbidden in ("password", "passwd", "secret", "token", "dsn", "username", "credentialblob"):
            self.assertNotIn(forbidden, rendered)

    def test_non_windows_fails_closed(self) -> None:
        with mock.patch.object(hhc.os, "name", "posix"):
            self.assertFalse(hhc.gateway_process_active())
            self.assertFalse(hhc.credential_present())

    def test_process_check_uses_no_shell(self) -> None:
        completed = mock.Mock(returncode=0, stdout=hhc.GATEWAY_IMAGE, stderr="")
        with mock.patch.object(hhc.os, "name", "nt"), mock.patch.object(hhc.subprocess, "run", return_value=completed) as run:
            self.assertTrue(hhc.gateway_process_active())
        self.assertIs(run.call_args.kwargs["shell"], False)


if __name__ == "__main__":
    unittest.main()

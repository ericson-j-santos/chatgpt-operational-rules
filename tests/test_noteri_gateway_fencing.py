"""Off-host real filesystem tests of local monotonic fencing; no Noteri mutation."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from scripts import command_gateway as gateway
from scripts import noteri_gateway_fencing as fencing

HOST = "noteri"
SHA = "a" * 64


class LocalFencingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="noteri-fencing-test-")
        self.addCleanup(self.tmp.cleanup)
        self.policy = {"state_dir": self.tmp.name}
        self.cwd = Path(self.tmp.name) / "worktree-a"
        self.second_cwd = Path(self.tmp.name) / "worktree-b"
        self.cwd.mkdir()
        self.second_cwd.mkdir()
        for patch in (
            mock.patch.object(fencing, "_local_host", return_value=True),
            mock.patch.object(fencing.socket, "gethostname", return_value=HOST),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        gateway.secure_state_directory(self.policy, "locks")
        gateway.secure_state_directory(self.policy, fencing.DIR)
        self.lock_a = self.add_lock("first.lock", self.cwd, "run-a")
        self.lock_b = self.add_lock("second.lock", self.second_cwd, "run-b")
        self.counter_path = gateway.secure_state_path(
            self.policy, fencing.DIR, fencing.COUNTER
        )
        self.active_path = gateway.secure_state_path(
            self.policy, fencing.DIR, fencing.ACTIVE
        )

    def add_lock(self, name, cwd, correlation):
        path = gateway.secure_state_path(self.policy, "locks", name)
        gateway.write_state_text(self.policy, path, json.dumps({
            "pid": os.getpid(), "host": HOST, "correlation_id": correlation,
            "repo_root": gateway.norm(cwd),
        }), exclusive=True)
        return path

    def provision_synthetic_counter(self, sequence=0, raw=None):
        gateway.write_state_text(
            self.policy, self.counter_path,
            raw if raw is not None else json.dumps(fencing._counter(sequence, HOST)),
            exclusive=True,
        )

    def issue(self, *, second=False, **changes):
        payload = {
            "policy": self.policy,
            "gateway_lock_path": self.lock_b if second else self.lock_a,
            "cwd": self.second_cwd if second else self.cwd,
            "correlation_id": "run-b" if second else "run-a",
            "session_id": "session-b" if second else "session-a",
            "snapshot_sha256": SHA,
        }
        payload.update(changes)
        return fencing.issue_local_token(**payload)

    def test_missing_counter_rejects_without_claim(self):
        with self.assertRaisesRegex(fencing.FencingBlocked, "not_provisioned"):
            self.issue()
        self.assertFalse(self.active_path.exists())

    def test_first_issued_token_is_durable_and_readable(self):
        self.provision_synthetic_counter()
        result = self.issue()
        self.assertEqual(result.sequence, 1)
        self.assertEqual(len(result.nonce), 32)
        self.assertTrue(fencing.verify_active_token(
            policy=self.policy, sequence=result.sequence, nonce=result.nonce,
            session_id="session-a", correlation_id="run-a",
        ))
        self.assertEqual(fencing._read_counter(
            self.policy, self.counter_path, HOST), 1)

    def test_two_worktrees_compete_for_same_target(self):
        self.provision_synthetic_counter()
        first = self.issue()
        with self.assertRaisesRegex(fencing.FencingBlocked, "already_claimed"):
            self.issue(second=True)
        self.assertEqual(first.sequence, 1)
        self.assertEqual(fencing._read_counter(
            self.policy, self.counter_path, HOST), 1)

    def test_true_parallel_competition_never_issues_twice(self):
        self.provision_synthetic_counter()
        def worker(second):
            try:
                return self.issue(second=second).sequence
            except fencing.FencingBlocked as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=2) as executor:
            outputs = list(executor.map(worker, [False, True]))
        self.assertEqual(outputs.count(1), 1)
        self.assertEqual(outputs.count("fence_already_claimed"), 1)
        self.assertEqual(fencing._read_counter(
            self.policy, self.counter_path, HOST), 1)

    def test_manual_reconciliation_in_test_never_resets_counter(self):
        self.provision_synthetic_counter()
        first = self.issue()
        # The test alone emulates a separately authorised recovery. Production
        # exposes no automatic unlink/recovery through this module.
        gateway.unlink_state_file(self.policy, self.active_path)
        second = self.issue(second=True)
        self.assertEqual((first.sequence, second.sequence), (1, 2))
        self.assertEqual(fencing._read_counter(
            self.policy, self.counter_path, HOST), 2)

    def test_corrupt_counter_is_not_reset_or_replaced(self):
        self.provision_synthetic_counter(raw="{not-json")
        with self.assertRaisesRegex(fencing.FencingBlocked, "unreadable"):
            self.issue()
        self.assertFalse(self.active_path.exists())
        self.assertEqual(gateway.read_state_text(
            self.policy, self.counter_path), "{not-json")

    def test_forged_counter_checksum_fails(self):
        data = fencing._counter(10, HOST)
        data["sha256"] = "0" * 64
        self.provision_synthetic_counter(raw=json.dumps(data))
        with self.assertRaisesRegex(fencing.FencingBlocked, "invalid"):
            self.issue()
        self.assertFalse(self.active_path.exists())

    def test_boolean_sequence_never_accepted(self):
        self.provision_synthetic_counter(raw=json.dumps(
            {**fencing._counter(1, HOST), "sequence": True}
        ))
        with self.assertRaisesRegex(fencing.FencingBlocked, "invalid"):
            self.issue()

    def test_corrupted_lock_identity_is_refused(self):
        with mock.patch.object(fencing.os, "getpid", return_value=os.getpid() + 1):
            with self.assertRaisesRegex(fencing.FencingBlocked, "not_owned"):
                self.issue()

    def test_wrong_lock_path_is_refused(self):
        self.provision_synthetic_counter()
        with self.assertRaisesRegex(fencing.FencingBlocked, "invalid_gateway_lock_path"):
            self.issue(gateway_lock_path=self.counter_path)
        self.assertFalse(self.active_path.exists())

    def test_wrong_host_blocks_before_state_mutation(self):
        self.provision_synthetic_counter()
        with mock.patch.object(fencing, "_local_host", return_value=False):
            with self.assertRaisesRegex(fencing.FencingBlocked, "wrong_host"):
                self.issue()
        self.assertFalse(self.active_path.exists())

    def test_failed_counter_write_keeps_claim_for_reconciliation(self):
        self.provision_synthetic_counter()
        with mock.patch.object(gateway, "atomic_write_state_text",
                               side_effect=OSError("synthetic IO failure")):
            with self.assertRaisesRegex(fencing.FencingBlocked, "failed"):
                self.issue()
        self.assertTrue(self.active_path.exists())
        with self.assertRaisesRegex(fencing.FencingBlocked, "already_claimed"):
            self.issue()
        self.assertEqual(fencing._read_counter(
            self.policy, self.counter_path, HOST), 0)

    def test_failed_readback_keeps_claim_instead_of_reissuing(self):
        self.provision_synthetic_counter()
        with mock.patch.object(gateway, "atomic_write_state_text"):
            with self.assertRaisesRegex(fencing.FencingBlocked, "readback_mismatch"):
                self.issue()
        self.assertTrue(self.active_path.exists())
        with self.assertRaisesRegex(fencing.FencingBlocked, "already_claimed"):
            self.issue()

    def test_stale_nonce_and_session_refused(self):
        self.provision_synthetic_counter()
        lease = self.issue()
        for props in (
            {"nonce": "0" * 32},
            {"session_id": "wrong"},
            {"correlation_id": "wrong"},
            {"sequence": lease.sequence + 1},
        ):
            with self.subTest(props=props):
                with self.assertRaisesRegex(fencing.FencingBlocked, "not_current_owner"):
                    fields = {
                        "policy": self.policy, "sequence": lease.sequence,
                        "nonce": lease.nonce, "session_id": "session-a",
                        "correlation_id": "run-a",
                    }
                    fields.update(props)
                    fencing.verify_active_token(**fields)

    def test_no_cli_no_auto_repair_no_cost_attestation(self):
        import inspect
        source = inspect.getsource(fencing)
        self.assertNotIn('if __name__ == "__main__":', source)
        self.assertNotIn("subprocess.run(", source)
        self.assertNotIn("zero_incremental_cost_verified", source)
        self.assertNotIn("unlink_state_file(", source)


if __name__ == "__main__":
    unittest.main()

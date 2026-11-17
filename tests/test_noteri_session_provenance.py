"""Tests for provenance joins; only synthetic local Gateway records."""
from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from scripts import noteri_session_provenance as p

NOW = datetime(2026, 11, 17, 2, 0, tzinfo=timezone.utc)
HEAD = "fd07d0742ad345efe934c7b800145765aaba6462"
DIGEST = "a" * 64
POLICY = "b" * 64
GATEWAY = "c" * 64
ROOT = Path("/synthetic/noteri")


def fixture():
    reservation = {
        "session_id": "session-01", "status": "materialized",
        "host": "noteri", "base_head": HEAD,
        "repo_root": str(ROOT), "reserved_worktree": str(ROOT / "worktree"),
        "correlation_id": "launch-01",
    }
    snapshot = {
        "timestamp": "2026-11-17T01:58:00Z", "result": "BOOTSTRAP_OK",
        "state_validated": True, "session_id": "session-01",
        "host": "noteri", "correlation_id": "launch-01",
        "repo_root": str(ROOT), "reserved_worktree": str(ROOT / "worktree"),
        "target_path": str(ROOT / "worktree"), "reservation_status": "materialized",
        "target_state": {"head": HEAD}, "gateway_sha256": GATEWAY,
        "policy_sha256": POLICY,
    }
    snapshot["snapshot_sha256"] = p.gateway.snapshot_sha256(snapshot)
    launch = {
        "timestamp": "2026-11-17T01:59:00Z", "action": "session_launcher",
        "result": "SESSION_LAUNCH_OK", "state_validated": True,
        "session_id": "session-01", "host": "noteri",
        "correlation_id": "launch-01", "snapshot_sha256": snapshot["snapshot_sha256"],
        "target_path": str(ROOT / "worktree"), "repo_root": str(ROOT),
        "head": HEAD,
    }
    lock = {
        "pid": os.getpid(), "host": "noteri",
        "correlation_id": "run-01", "repo_root": str(ROOT / "worktree"),
    }
    return reservation, snapshot, launch, lock


class SessionProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.reservation, self.snapshot, self.launch, self.lock = fixture()
        self.events = [self.launch]
        self.policy = {"require_session_bootstrap": True,
                       "require_preflight_snapshot": True}
        self.patches = [
            mock.patch.object(p.socket, "gethostname", return_value="noteri"),
            mock.patch.object(p.gateway, "sha256_file", return_value=GATEWAY),
            mock.patch.object(p.gateway, "secure_state_path", side_effect=
                lambda policy, name, *rest: Path("/state") / name),
            mock.patch.object(p.gateway, "read_state_text", side_effect=self._read),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _read(self, policy, path):
        if str(path).endswith("gateway.lock"):
            return json.dumps(self.lock)
        return "".join(json.dumps(e) + "\n" for e in self.events)

    def prove(self, **overrides):
        args = {
            "policy": self.policy, "reservation": self.reservation,
            "snapshot": self.snapshot, "lock_path": Path("/state/gateway.lock"),
            "cwd": ROOT / "worktree", "expected_head": HEAD,
            "session_id": "session-01", "correlation_id": "run-01",
            "policy_file_sha256": POLICY, "now": NOW,
        }
        args.update(overrides)
        return p.verify_launcher_and_lock(**args)

    def test_reuses_real_launcher_and_exclusive_lock_records(self):
        proof = self.prove()
        self.assertTrue(proof["session_launch_ok"])
        self.assertTrue(proof["exclusive_lease"])
        self.assertEqual(proof["session_snapshot_sha256"], self.snapshot["snapshot_sha256"])
        self.assertEqual(proof["policy_sha256"], POLICY)
        self.assertNotIn("fencing_token", proof)
        self.assertNotIn("zero_incremental_cost_verified", proof)

    def test_absent_launcher_event_fails(self):
        self.events = []
        with self.assertRaisesRegex(p.ProvenanceBlocked, "not_unique"):
            self.prove()

    def test_replayed_launcher_event_fails(self):
        self.events.append(dict(self.launch))
        with self.assertRaisesRegex(p.ProvenanceBlocked, "not_unique"):
            self.prove()

    def test_wrong_host_or_sha_fails(self):
        for key, value in [("host", "different"), ("base_head", "d" * 40)]:
            with self.subTest(key=key):
                original = self.reservation[key]
                self.reservation[key] = value
                with self.assertRaises(p.ProvenanceBlocked):
                    self.prove()
                self.reservation[key] = original

    def test_expired_or_future_launcher_event_fails(self):
        for stamp in ["2026-11-16T20:00:00Z", "2026-11-17T03:00:00Z"]:
            with self.subTest(stamp=stamp):
                self.launch["timestamp"] = stamp
                with self.assertRaisesRegex(p.ProvenanceBlocked, "expired"):
                    self.prove()

    def test_snapshot_tamper_fails(self):
        self.snapshot["target_state"]["head"] = "d" * 40
        with self.assertRaisesRegex(p.ProvenanceBlocked, "snapshot_not_verified"):
            self.prove()

    def test_policy_or_gateway_drift_fails(self):
        with self.assertRaisesRegex(p.ProvenanceBlocked, "not_pinned"):
            self.prove(policy_file_sha256="d" * 64)
        with mock.patch.object(p.gateway, "sha256_file", return_value="e" * 64):
            with self.assertRaisesRegex(p.ProvenanceBlocked, "not_pinned"):
                self.prove()

    def test_lock_wrong_identity_or_correlation_fails(self):
        for field, changed in [("pid", os.getpid() + 1),
                               ("correlation_id", "other-run")]:
            with self.subTest(field=field):
                previous = self.lock[field]
                self.lock[field] = changed
                with self.assertRaisesRegex(p.ProvenanceBlocked, "lock_not_owned"):
                    self.prove()
                self.lock[field] = previous

    def test_corrupt_log_fails_closed(self):
        self.events.append("not-a-dict")
        with self.assertRaisesRegex(p.ProvenanceBlocked, "mismatch"):
            self.prove()

    def test_bad_snapshot_flags_block(self):
        with self.assertRaisesRegex(p.ProvenanceBlocked, "not_verified"):
            self.prove(policy={"require_session_bootstrap": False,
                               "require_preflight_snapshot": True})

    def test_no_cost_or_fencing_derived_from_launcher_log(self):
        self.launch["zero_incremental_cost_verified"] = True
        self.launch["fencing_token"] = 999
        proof = self.prove()
        self.assertNotIn("zero_incremental_cost_verified", proof)
        self.assertNotIn("fencing_token", proof)


if __name__ == "__main__":
    unittest.main()

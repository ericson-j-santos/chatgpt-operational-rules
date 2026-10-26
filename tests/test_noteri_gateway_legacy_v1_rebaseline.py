"""Fail-closed read-only gates for Noteri's historical v1 Gateway backup.

These fixtures model v1's seven managed files and a blocked update receipt.
No real Noteri installation, WSL, elevated command or service is touched.
"""
from __future__ import annotations

import hashlib
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import noteri_gateway_legacy_v1_rebaseline as legacy


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LegacyNoteriRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "install"
        self.root.mkdir()
        self.backup = self.root / ("backup-20261019-160829-" + "a" * 32)
        self.backup.mkdir()
        self.expected = {}
        entries = {}
        for source, dest in legacy.bootstrap.RUNTIME_MAP.items():
            existing = dest != "bin/runner_version_preflight.py"
            item = {
                "source": source, "existed": existing,
                "desired_sha256": "b" * 64, "desired_size": 300,
            }
            if existing:
                payload = ("previous-" + dest + "\n").encode()
                target = self.root / dest
                stored = self.backup / "files" / dest
                target.parent.mkdir(parents=True, exist_ok=True)
                stored.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                stored.write_bytes(payload)
                mode = stat.S_IMODE(target.stat().st_mode)
                stored.chmod(mode)
                item.update(previous_sha256=digest(payload), previous_size=len(payload),
                            previous_mode=mode)
                self.expected[dest] = payload
            entries[dest] = item
        self.manifest = {
            "version": 1, "install_root": str(self.root.resolve()),
            "entries": entries, "created_at": "2026-10-19T19:08:29Z",
        }
        self.receipt = {
            "result": "HOST_UPDATE_BLOCKED",
            "source_commit": legacy.LEGACY_SOURCE_COMMIT,
            "backup_dir": str(self.backup),
            "rollback_attempted": True, "rollback_succeeded": False,
        }
        self.manifest_path = self.backup / "rollback-manifest.json"
        self.receipt_path = self.root / "update-receipt.json"
        self.write()

    def write(self):
        self.manifest_path.write_text(
            json.dumps(self.manifest, sort_keys=True) + "\n", encoding="utf-8")
        self.receipt_path.write_text(
            json.dumps(self.receipt, sort_keys=True) + "\n", encoding="utf-8")
        self.pin_receipt = digest(self.receipt_path.read_bytes())
        self.pin_manifest = digest(self.manifest_path.read_bytes())

    def verify(self, **pins):
        metadata = {
            "version": 1, "attributes": 32,
            "security": {"simulated": True}, "streams": [],
        }
        with (
            mock.patch.object(legacy, "_host_is_noteri_windows", return_value=True),
            mock.patch.object(legacy, "_required_install_root", return_value=self.root),
            mock.patch.object(legacy.bootstrap, "_windows_metadata_required", return_value=True),
            mock.patch.object(legacy.bootstrap, "_windows_persisted_file_state",
                              return_value=metadata) as native,
            mock.patch.object(legacy.bootstrap, "_validate_windows_persisted_state"),
            mock.patch.object(legacy.bootstrap, "_windows_persisted_state_equivalent",
                              side_effect=lambda a, b: a == b),
        ):
            return legacy.verify_legacy_v1_current_state(
                self.root,
                expected_receipt_sha256=pins.get("receipt", self.pin_receipt),
                expected_manifest_sha256=pins.get("manifest", self.pin_manifest),
            )

    def test_positive_and_replay_preserve_every_byte(self):
        before = {
            str(p.relative_to(self.root)): p.read_bytes()
            for p in self.root.rglob("*") if p.is_file()
        }
        first = self.verify()
        second = self.verify()
        after = {
            str(p.relative_to(self.root)): p.read_bytes()
            for p in self.root.rglob("*") if p.is_file()
        }
        self.assertEqual(before, after)
        self.assertEqual(first, second)
        self.assertEqual(first["existing_file_count"], 6)
        self.assertEqual(first["absent_file_count"], 1)
        self.assertTrue(first["current_windows_metadata_readable"])
        for name in ("historical_windows_metadata_verified",
                     "new_v2_snapshot_created", "managed_runtime_changed",
                     "installation_authorized"):
            self.assertFalse(first[name])

    def test_mismatched_receipt_pin_fails_closed(self):
        with self.assertRaisesRegex(legacy.LegacyBlocked, "LEGACY_RECEIPT_DRIFT"):
            self.verify(receipt="1" * 64)

    def test_mismatched_manifest_pin_fails_closed(self):
        with self.assertRaisesRegex(legacy.LegacyBlocked, "LEGACY_MANIFEST_DRIFT"):
            self.verify(manifest="1" * 64)

    def test_missing_pins_fail_before_filesystem_snapshot(self):
        with self.assertRaisesRegex(legacy.LegacyBlocked, "LEGACY_RECEIPT_PIN_INVALID"):
            self.verify(receipt="not-a-hash")
        with self.assertRaisesRegex(legacy.LegacyBlocked, "LEGACY_MANIFEST_PIN_INVALID"):
            self.verify(manifest="")

    def test_old_receipt_not_blocked_or_other_commit_is_denied(self):
        for key, value in (
            ("result", "HOST_UPDATE_OK"),
            ("source_commit", "c" * 40),
            ("rollback_attempted", False),
            ("rollback_succeeded", True),
        ):
            with self.subTest(key=key):
                saved = dict(self.receipt)
                self.receipt[key] = value
                self.write()
                with self.assertRaisesRegex(legacy.LegacyBlocked,
                                             "LEGACY_RECEIPT_STATE_INVALID"):
                    self.verify()
                self.receipt = saved
                self.write()

    def test_legacy_v1_never_accepts_claimed_v2_metadata(self):
        self.manifest["metadata_contract"] = "windows-owner-group-dacl-ads-v1"
        self.write()
        with self.assertRaisesRegex(legacy.LegacyBlocked, "LEGACY_MANIFEST_HEADER_INVALID"):
            self.verify()

    def test_tracked_bytes_changed_without_manifest_change_blocks(self):
        target = self.root / "bin/command_gateway.py"
        target.write_bytes(b"tampered\n")
        with self.assertRaisesRegex(legacy.LegacyBlocked, "LEGACY_CONTENT_OR_MODE_DRIFT"):
            self.verify()

    def test_saved_backup_bytes_changed_blocks(self):
        target = self.backup / "files/bin/session_launcher.py"
        target.write_bytes(b"tampered-backup")
        with self.assertRaisesRegex(legacy.LegacyBlocked, "LEGACY_CONTENT_OR_MODE_DRIFT"):
            self.verify()

    def test_new_managed_file_from_absent_v1_blocks(self):
        target = self.root / "bin/runner_version_preflight.py"
        target.write_bytes(b"not-original\n")
        with self.assertRaisesRegex(legacy.LegacyBlocked, "LEGACY_UNEXPECTED_FILE"):
            self.verify()

    def test_wrong_backup_path_is_denied(self):
        self.receipt["backup_dir"] = str(self.root.parent)
        self.write()
        with self.assertRaisesRegex(legacy.LegacyBlocked, "LEGACY_BACKUP_REFERENCE_INVALID"):
            self.verify()

    def test_missing_managed_entry_blocks(self):
        self.manifest["entries"].pop("bin/session_launcher.py")
        self.write()
        with self.assertRaisesRegex(legacy.LegacyBlocked, "LEGACY_MANAGED_SET_INVALID"):
            self.verify()

    def test_unexpected_manifest_source_blocks(self):
        self.manifest["entries"]["bin/command_gateway.py"]["source"] = "scripts/other.py"
        self.write()
        with self.assertRaisesRegex(legacy.LegacyBlocked, "LEGACY_ENTRY_INVALID"):
            self.verify()

    def test_fail_closed_if_real_host_guard_fails(self):
        with mock.patch.object(legacy, "_host_is_noteri_windows", return_value=False):
            with self.assertRaisesRegex(legacy.LegacyBlocked, "LEGACY_NOTERI_WINDOWS_REQUIRED"):
                legacy.verify_legacy_v1_current_state(
                    self.root, expected_receipt_sha256=self.pin_receipt,
                    expected_manifest_sha256=self.pin_manifest)

    def test_native_metadata_read_failure_blocks(self):
        with (
            mock.patch.object(legacy, "_host_is_noteri_windows", return_value=True),
            mock.patch.object(legacy, "_required_install_root", return_value=self.root),
            mock.patch.object(legacy.bootstrap, "_windows_metadata_required", return_value=True),
            mock.patch.object(legacy.bootstrap, "_windows_persisted_file_state",
                              side_effect=PermissionError("hidden private path")),
        ):
            with self.assertRaisesRegex(legacy.LegacyBlocked,
                                         "LEGACY_CURRENT_METADATA_UNAVAILABLE"):
                legacy.verify_legacy_v1_current_state(
                    self.root, expected_receipt_sha256=self.pin_receipt,
                    expected_manifest_sha256=self.pin_manifest)

    def test_preflight_never_invokes_wsl_or_mutates_managed_files(self):
        code = (ROOT / "scripts/noteri_gateway_legacy_v1_rebaseline.py").read_text()
        for forbidden in (
            "wsl.exe", "shell=True", "subprocess.Popen", "subprocess.run",
            "os.system(", "setfacl", "icacls", "StartServiceW", "shutdown",
        ):
            self.assertNotIn(forbidden, code)


if __name__ == "__main__":
    unittest.main()

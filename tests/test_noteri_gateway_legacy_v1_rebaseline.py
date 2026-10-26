"""Fail-closed read-only gates for Noteri's historical v1 Gateway backup.

These fixtures model v1's seven managed files and a blocked update receipt.
No real Noteri installation, WSL, elevated command or service is touched.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import noteri_gateway_legacy_v1_rebaseline as legacy
import update_command_gateway_host as updater


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


    def _update_argv(self, commit: str, *, receipt=None, manifest=None):
        return [
            "update_command_gateway_host.py", "--commit", commit,
            "--expected-self-sha256", updater.sha256_file(Path(updater.__file__).resolve()),
            "--install-root", str(self.root),
            "--work-root", str(Path(self.temp.name) / "workers"),
            "--noteri-legacy-v1-rebaseline",
            "--expected-legacy-receipt-sha256", receipt or self.pin_receipt,
            "--expected-legacy-manifest-sha256", manifest or self.pin_manifest,
        ]

    def _fake_verified_bundle(self, commit: str):
        def download(observed: str, destination: Path):
            self.assertEqual(observed, commit)
            (destination / "MANIFEST.json").write_bytes((ROOT / "MANIFEST.json").read_bytes())
            for src in legacy.bootstrap.RUNTIME_MAP:
                dst = destination / src
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / src, dst)
        return download

    def test_v1_rebaseline_stages_authentic_v2_then_denies_locked_target(self):
        old_receipt = self.receipt_path.read_bytes()
        old_manifest = self.manifest_path.read_bytes()
        old_files = dict(self.expected)
        commit = "d" * 40
        with (
            mock.patch.object(legacy, "verify_legacy_v1_current_state",
                              return_value={"result": "LEGACY_CURRENT_STATE_VERIFIED"}) as gate,
            mock.patch.object(updater.bootstrap, "download_bundle",
                              side_effect=self._fake_verified_bundle(commit)),
            mock.patch.object(updater, "preflight_runtime_replaceability",
                              side_effect=updater.RuntimeReplaceabilityError(
                                  "bin/session_launcher.py", "delete_access_os_error",
                                  "simulated access denied",
                              )) as denied,
            mock.patch.object(sys, "argv", self._update_argv(commit)),
        ):
            self.assertEqual(updater.main(), updater.EXIT_HOST_UPDATE)
        gate.assert_called_once()
        denied.assert_called_once()
        new_receipt = json.loads(self.receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(new_receipt["phase"], "replaceability_preflight")
        self.assertFalse(new_receipt["mutation_started"])
        self.assertFalse(new_receipt["rollback_attempted"])
        self.assertFalse(new_receipt["rollback_succeeded"] is True)
        self.assertTrue(new_receipt["legacy_v1_rebaseline"]["admitted"])
        self.assertTrue(new_receipt["legacy_v1_rebaseline"]["original_receipt_archived"])
        self.assertFalse(new_receipt["legacy_v1_rebaseline"]["historical_metadata_verified"])
        fresh = Path(new_receipt["backup_dir"])
        self.assertNotEqual(fresh, self.backup)
        self.assertEqual((fresh / "legacy-update-receipt.json").read_bytes(), old_receipt)
        snapshot = legacy.bootstrap._load_runtime_rollback(self.root, fresh)
        self.assertEqual(snapshot["version"], legacy.bootstrap.ROLLBACK_MANIFEST_VERSION)
        self.assertEqual(snapshot["metadata_contract"],
                         (legacy.bootstrap.WINDOWS_ROLLBACK_METADATA_CONTRACT
                          if legacy.bootstrap._windows_metadata_required()
                          else legacy.bootstrap.POSIX_ROLLBACK_METADATA_CONTRACT))
        self.assertEqual(self.manifest_path.read_bytes(), old_manifest)
        for name, value in old_files.items():
            self.assertEqual((self.root / name).read_bytes(), value)
        self.assertFalse((self.root / "bin/runner_version_preflight.py").exists())

    def test_invalid_legacy_pin_never_overwrites_original_failed_receipt(self):
        before = self.receipt_path.read_bytes()
        commit = "d" * 40
        with (
            mock.patch.object(legacy, "verify_legacy_v1_current_state",
                              side_effect=legacy.LegacyBlocked("LEGACY_RECEIPT_DRIFT")),
            mock.patch.object(updater.bootstrap, "download_bundle") as download,
            mock.patch.object(sys, "argv", self._update_argv(commit)),
        ):
            self.assertEqual(updater.main(), updater.EXIT_HOST_UPDATE)
        download.assert_not_called()
        self.assertEqual(self.receipt_path.read_bytes(), before)

    def test_incomplete_legacy_rebaseline_pins_fail_before_update_lock(self):
        before = self.receipt_path.read_bytes()
        argv = self._update_argv("d" * 40)[:-2]
        with (
            mock.patch.object(updater, "acquire_update_lock") as lock,
            mock.patch.object(sys, "argv", argv),
        ):
            self.assertEqual(updater.main(), updater.EXIT_HOST_UPDATE)
        lock.assert_not_called()
        self.assertEqual(before, self.receipt_path.read_bytes())

    def test_legacy_opt_in_rejected_when_latest_receipt_is_not_blocked(self):
        self.receipt["result"] = "HOST_UPDATE_OK"
        self.write()
        original = self.receipt_path.read_bytes()
        with (
            mock.patch.object(legacy, "verify_legacy_v1_current_state") as gate,
            mock.patch.object(updater.bootstrap, "download_bundle") as downloader,
            mock.patch.object(sys, "argv", self._update_argv("d" * 40)),
        ):
            self.assertEqual(updater.main(), updater.EXIT_HOST_UPDATE)
        gate.assert_not_called()
        downloader.assert_not_called()
        self.assertEqual(self.receipt_path.read_bytes(), original)



    def test_stage_only_v2_is_idempotent_and_does_not_install(self):
        original = self.receipt_path.read_bytes()
        commit = "d" * 40
        argv = self._update_argv(commit) + ["--noteri-legacy-v1-stage-only"]
        readback_ok = {"result": "LEGACY_CURRENT_STATE_VERIFIED"}
        with (
            mock.patch.object(legacy, "verify_legacy_v1_current_state",
                              return_value=readback_ok) as verify,
            mock.patch.object(updater.bootstrap, "download_bundle",
                              side_effect=self._fake_verified_bundle(commit)) as download,
            mock.patch.object(updater, "preflight_runtime_replaceability",
                              side_effect=updater.RuntimeReplaceabilityError(
                                  "bin/session_launcher.py", "delete_access_os_error",
                                  "simulated WinError 5",
                              )),
            mock.patch.object(updater.bootstrap, "install_bundle") as installer,
            mock.patch.object(sys, "argv", argv),
        ):
            self.assertEqual(updater.main(), 0)
            stages = list(self.root.glob("backup-*/legacy-stage-receipt.json"))
            self.assertEqual(len(stages), 1)
            self.assertEqual(self.receipt_path.read_bytes(), original)
            receipt = json.loads(stages[0].read_text(encoding="utf-8"))
            self.assertEqual(receipt["result"], "HOST_LEGACY_V2_STAGED_ONLY")
            self.assertEqual(receipt["replacement_preflight"], "delete_access_os_error")
            self.assertFalse(receipt["replaceability_proved"])
            self.assertFalse(receipt["installation_performed"])
            self.assertEqual((stages[0].parent / "legacy-update-receipt.json").read_bytes(),
                             original)
            self.assertEqual(updater.main(), 0)
            self.assertEqual(list(self.root.glob("backup-*/legacy-stage-receipt.json")),
                             stages)
        self.assertEqual(verify.call_count, 2)
        download.assert_called_once()
        installer.assert_not_called()
        self.assertEqual(self.receipt_path.read_bytes(), original)
        for name, payload in self.expected.items():
            self.assertEqual((self.root / name).read_bytes(), payload)

    def test_stage_only_denies_divergent_replay_without_overwriting_v1(self):
        commit = "d" * 40
        argv = self._update_argv(commit) + ["--noteri-legacy-v1-stage-only"]
        before = self.receipt_path.read_bytes()
        with (
            mock.patch.object(legacy, "verify_legacy_v1_current_state",
                              return_value={"result": "LEGACY_CURRENT_STATE_VERIFIED"}),
            mock.patch.object(updater.bootstrap, "download_bundle",
                              side_effect=self._fake_verified_bundle(commit)),
            mock.patch.object(updater, "preflight_runtime_replaceability",
                              side_effect=updater.RuntimeReplaceabilityError(
                                  "bin/session_launcher.py", "delete_access_os_error",
                                  "simulated denial")),
            mock.patch.object(sys, "argv", argv),
        ):
            self.assertEqual(updater.main(), 0)
            stage = next(self.root.glob("backup-*/legacy-stage-receipt.json"))
            data = json.loads(stage.read_text(encoding="utf-8"))
            data["v2_manifest_sha256"] = "0" * 64
            stage.write_text(json.dumps(data), encoding="utf-8")
            with mock.patch.object(updater.bootstrap, "download_bundle") as download:
                self.assertEqual(updater.main(), updater.EXIT_HOST_UPDATE)
                download.assert_not_called()
        self.assertEqual(self.receipt_path.read_bytes(), before)

    def test_stage_only_flag_without_explicit_rebaseline_is_denied(self):
        before = self.receipt_path.read_bytes()
        argv = self._update_argv("d" * 40)
        argv.remove("--noteri-legacy-v1-rebaseline")
        argv.append("--noteri-legacy-v1-stage-only")
        with (
            mock.patch.object(updater, "acquire_update_lock") as lock,
            mock.patch.object(sys, "argv", argv),
        ):
            self.assertEqual(updater.main(), updater.EXIT_HOST_UPDATE)
        lock.assert_not_called()
        self.assertEqual(self.receipt_path.read_bytes(), before)



if __name__ == "__main__":
    unittest.main()

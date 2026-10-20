from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import update_command_gateway_host as updater


class HostRuntimeUpdateTests(unittest.TestCase):
    def make_current_bundle(self, root: Path) -> Path:
        bundle = root / "current-bundle"
        bundle.mkdir()
        shutil.copy2(ROOT / "MANIFEST.json", bundle / "MANIFEST.json")
        for source in updater.bootstrap.RUNTIME_MAP:
            target = bundle / source
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / source, target)
        return bundle

    def seed_legacy_runtime(
        self, install: Path
    ) -> tuple[dict[str, tuple[bytes, int]], Path, int]:
        previous: dict[str, tuple[bytes, int]] = {}
        for source, destination in updater.bootstrap.RUNTIME_MAP.items():
            if source == "scripts/runner_version_preflight.py":
                continue
            target = install / destination
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = f"legacy:{destination}\n".encode("utf-8")
            target.write_bytes(payload)
            target.chmod(0o640)
            observed_mode = stat.S_IMODE(target.stat().st_mode)
            if os.name != "nt":
                self.assertEqual(observed_mode, 0o640)
            previous[destination] = (payload, observed_mode)
        unmanaged = install / "unmanaged.keep"
        unmanaged.write_bytes(b"preserve-me\n")
        unmanaged.chmod(0o600)
        unmanaged_mode = stat.S_IMODE(unmanaged.stat().st_mode)
        if os.name != "nt":
            self.assertEqual(unmanaged_mode, 0o600)
        return previous, unmanaged, unmanaged_mode

    def assert_legacy_state(
        self,
        install: Path,
        previous: dict[str, tuple[bytes, int]],
        unmanaged: Path,
        unmanaged_mode: int,
    ) -> None:
        for destination, (payload, mode) in previous.items():
            target = install / destination
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), mode)
        self.assertFalse(
            (install / "bin" / "runner_version_preflight.py").exists()
        )
        self.assertEqual(unmanaged.read_bytes(), b"preserve-me\n")
        self.assertEqual(
            stat.S_IMODE(unmanaged.stat().st_mode), unmanaged_mode
        )

    def update_argv(
        self, install: Path, work_root: Path, commit: str
    ) -> list[str]:
        return [
            "update_command_gateway_host.py",
            "--commit",
            commit,
            "--expected-self-sha256",
            updater.sha256_file(Path(updater.__file__).resolve()),
            "--install-root",
            str(install),
            "--work-root",
            str(work_root),
        ]

    def copy_bundle(self, source: Path, commit: str):
        def copy_verified_bundle(observed_commit: str, target: Path) -> None:
            self.assertEqual(observed_commit, commit)
            shutil.copytree(source, target, dirs_exist_ok=True)

        return copy_verified_bundle

    def test_receipt_writer_is_atomic_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "receipt.json"
            updater.atomic_json(
                target, {"result": "HOST_UPDATE_OK", "secret": False}
            )
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(payload["result"], "HOST_UPDATE_OK")
            self.assertFalse(payload["secret"])

    def test_sha256_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "x"
            target.write_bytes(b"abc")
            self.assertEqual(
                updater.sha256_file(target),
                hashlib.sha256(b"abc").hexdigest(),
            )

    def test_restore_without_backup_does_not_mutate(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = Path(tmp) / "install"
            updater.restore_backup(install, None)
            self.assertFalse(install.exists())

    def test_lock_release_error_does_not_override_blocked_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = root / "install"
            work_root = root / "workers"
            commit = "0" * 40
            argv = self.update_argv(install, work_root, commit)
            held = mock.Mock()
            held.close.side_effect = OSError("injected lock release failure")

            with mock.patch.object(
                updater,
                "acquire_update_lock",
                return_value=held,
            ), mock.patch.object(sys, "argv", argv):
                self.assertEqual(updater.main(), updater.EXIT_HOST_UPDATE)

            held.close.assert_called_once_with()

    def test_read_only_target_blocks_before_desired_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = root / "install"
            work_root = root / "workers"
            bundle = self.make_current_bundle(root)
            self.seed_legacy_runtime(install)
            target = install / "bin" / "command_gateway.py"
            previous = target.read_bytes()
            target.chmod(stat.S_IREAD)
            commit = "1" * 40
            argv = self.update_argv(install, work_root, commit)
            try:
                with mock.patch.object(
                    updater.bootstrap,
                    "download_bundle",
                    side_effect=self.copy_bundle(bundle, commit),
                ), mock.patch.object(sys, "argv", argv):
                    self.assertEqual(
                        updater.main(), updater.EXIT_HOST_UPDATE
                    )
                self.assertEqual(target.read_bytes(), previous)
                receipt = json.loads(
                    (install / "update-receipt.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(receipt["phase"], "replaceability_preflight")
                self.assertFalse(receipt["mutation_started"])
                self.assertFalse(receipt["rollback_attempted"])
                self.assertIsNone(receipt["rollback_succeeded"])
                self.assertTrue(receipt["reconciliation"]["previous_state"])
                self.assertEqual(receipt["failure"]["category"], "read_only")
                self.assertTrue(receipt["failure"]["cause_determined"])

                target.chmod(stat.S_IREAD | stat.S_IWRITE)
                with mock.patch.object(
                    updater.bootstrap,
                    "download_bundle",
                    side_effect=self.copy_bundle(bundle, commit),
                ), mock.patch.object(
                    updater.bootstrap,
                    "resolve_git",
                    return_value=(Path(sys.executable), "test"),
                ), mock.patch.object(
                    updater.bootstrap,
                    "prepare_validation_repo",
                    return_value=(work_root / "validation", commit),
                ), mock.patch.object(sys, "argv", argv):
                    self.assertEqual(updater.main(), 0)

                recovered = json.loads(
                    (install / "update-receipt.json").read_text(encoding="utf-8")
                )
                self.assertEqual(recovered["result"], "HOST_UPDATE_OK")
                self.assertTrue(recovered["reconciliation"]["desired_state"])
                self.assertFalse((install / "update-transaction.json").exists())
            finally:
                target.chmod(stat.S_IREAD | stat.S_IWRITE)

    def test_blocked_receipt_uses_recovery_file_if_primary_write_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = root / "install"
            work_root = root / "workers"
            bundle = self.make_current_bundle(root)
            self.seed_legacy_runtime(install)
            target = install / "bin" / "command_gateway.py"
            target.chmod(stat.S_IREAD)
            commit = "8" * 40
            argv = self.update_argv(install, work_root, commit)
            try:
                with mock.patch.object(
                    updater.bootstrap,
                    "download_bundle",
                    side_effect=self.copy_bundle(bundle, commit),
                ), mock.patch.object(
                    updater,
                    "atomic_json",
                    side_effect=OSError("injected-primary-receipt-failure"),
                ), mock.patch.object(sys, "argv", argv):
                    self.assertEqual(
                        updater.main(), updater.EXIT_HOST_UPDATE
                    )
                self.assertFalse((install / "update-receipt.json").exists())
                fallbacks = list(install.glob("update-recovery-*.json"))
                self.assertEqual(len(fallbacks), 1)
                receipt = json.loads(
                    fallbacks[0].read_text(encoding="utf-8")
                )
                self.assertIn(
                    "injected-primary-receipt-failure",
                    receipt["primary_receipt_error"],
                )
                self.assertTrue(receipt["reconciliation"]["previous_state"])
                self.assertFalse(receipt["mutation_started"])
            finally:
                target.chmod(stat.S_IREAD | stat.S_IWRITE)

    def test_replace_denial_is_not_retried_or_misattributed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = root / "install"
            work_root = root / "workers"
            bundle = self.make_current_bundle(root)
            self.seed_legacy_runtime(install)
            target = (install / "bin" / "command_gateway.py").resolve()
            commit = "2" * 40
            argv = self.update_argv(install, work_root, commit)
            real_replace = updater.bootstrap.replace_file
            denied_calls = 0

            def deny_probe(source, destination):
                nonlocal denied_calls
                if (
                    Path(source).parent.resolve() == target.parent
                    and Path(source).name.startswith(
                        target.name + ".replace-probe-"
                    )
                    and ".replace-probe-" in Path(destination).name
                ):
                    denied_calls += 1
                    raise PermissionError(
                        13, "simulated sharing, ACL, or security denial"
                    )
                return real_replace(source, destination)

            with mock.patch.object(
                updater.bootstrap,
                "download_bundle",
                side_effect=self.copy_bundle(bundle, commit),
            ), mock.patch.object(
                updater.bootstrap, "replace_file", side_effect=deny_probe
            ), mock.patch.object(sys, "argv", argv):
                self.assertEqual(updater.main(), updater.EXIT_HOST_UPDATE)

            first_receipt = json.loads(
                (install / "update-receipt.json").read_text(encoding="utf-8")
            )
            with mock.patch.object(
                updater.bootstrap,
                "download_bundle",
                side_effect=self.copy_bundle(bundle, commit),
            ), mock.patch.object(
                updater.bootstrap, "replace_file", side_effect=deny_probe
            ), mock.patch.object(sys, "argv", argv):
                self.assertEqual(updater.main(), updater.EXIT_HOST_UPDATE)

            self.assertEqual(denied_calls, 2)
            receipt = json.loads(
                (install / "update-receipt.json").read_text(encoding="utf-8")
            )
            self.assertNotEqual(
                first_receipt["transaction_id"], receipt["transaction_id"]
            )
            self.assertFalse(receipt["failure"]["cause_determined"])
            self.assertEqual(
                receipt["failure"]["category"], "replace_os_error"
            )
            self.assertEqual(
                receipt["failure"]["retry_policy"],
                "requires_changed_precondition_and_new_transaction",
            )
            self.assertIn(
                "open_handle_without_delete_share",
                receipt["failure"]["candidate_causes"],
            )
            self.assertIn(
                "acl_or_directory_permission",
                receipt["failure"]["candidate_causes"],
            )
            self.assertIn(
                "security_or_antivirus_interference",
                receipt["failure"]["candidate_causes"],
            )
            self.assertTrue(receipt["reconciliation"]["previous_state"])

    def test_transient_replace_failure_succeeds_only_in_new_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = root / "install"
            work_root = root / "workers"
            bundle = self.make_current_bundle(root)
            self.seed_legacy_runtime(install)
            target = (install / "bin" / "command_gateway.py").resolve()
            commit = "3" * 40
            argv = self.update_argv(install, work_root, commit)
            real_replace = updater.bootstrap.replace_file

            def transient_denial(source, destination):
                if (
                    Path(source).parent.resolve() == target.parent
                    and Path(source).name.startswith(
                        target.name + ".replace-probe-"
                    )
                    and ".replace-probe-" in Path(destination).name
                ):
                    raise PermissionError(13, "transient simulated denial")
                return real_replace(source, destination)

            with mock.patch.object(
                updater.bootstrap,
                "download_bundle",
                side_effect=self.copy_bundle(bundle, commit),
            ), mock.patch.object(
                updater.bootstrap,
                "replace_file",
                side_effect=transient_denial,
            ), mock.patch.object(sys, "argv", argv):
                self.assertEqual(updater.main(), updater.EXIT_HOST_UPDATE)
            blocked = json.loads(
                (install / "update-receipt.json").read_text(encoding="utf-8")
            )

            with mock.patch.object(
                updater.bootstrap,
                "download_bundle",
                side_effect=self.copy_bundle(bundle, commit),
            ), mock.patch.object(
                updater.bootstrap,
                "resolve_git",
                return_value=(Path(sys.executable), "test"),
            ), mock.patch.object(
                updater.bootstrap,
                "prepare_validation_repo",
                return_value=(work_root / "validation", commit),
            ), mock.patch.object(sys, "argv", argv):
                self.assertEqual(updater.main(), 0)
            success = json.loads(
                (install / "update-receipt.json").read_text(encoding="utf-8")
            )
            self.assertNotEqual(
                blocked["transaction_id"], success["transaction_id"]
            )
            self.assertEqual(success["result"], "HOST_UPDATE_OK")

    @unittest.skipUnless(os.name == "nt", "requer sharing modes do Windows")
    def test_windows_handle_without_delete_share_is_caught_by_preflight(self):
        import ctypes
        from ctypes import wintypes

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = root / "install"
            bundle = self.make_current_bundle(root)
            self.seed_legacy_runtime(install)
            backup = updater.bootstrap.prepare_runtime_rollback(bundle, install)
            target = install / "bin" / "command_gateway.py"
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_file = kernel32.CreateFileW
            create_file.argtypes = [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            ]
            create_file.restype = wintypes.HANDLE
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [wintypes.HANDLE]
            close_handle.restype = wintypes.BOOL
            handle = create_file(
                str(target),
                0x80000000,
                0x00000001 | 0x00000002,
                None,
                3,
                0x00000080,
                None,
            )
            self.assertNotEqual(handle, wintypes.HANDLE(-1).value)
            try:
                with self.assertRaises(updater.RuntimeReplaceabilityError) as caught:
                    updater.preflight_runtime_replaceability(install, backup)
                self.assertEqual(
                    caught.exception.category, "delete_access_os_error"
                )
            finally:
                self.assertTrue(close_handle(handle))

    @unittest.skipUnless(os.name == "nt", "requer ReplaceFileW do Windows")
    def test_windows_replace_preserves_security_descriptor_and_ads(self):
        import ctypes
        from ctypes import wintypes

        def security_descriptor(path: Path) -> bytes:
            kernel32 = ctypes.WinDLL("advapi32", use_last_error=True)
            get_security = kernel32.GetFileSecurityW
            get_security.argtypes = [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
            ]
            get_security.restype = wintypes.BOOL
            needed = wintypes.DWORD()
            information = 0x00000001 | 0x00000002 | 0x00000004
            get_security(str(path), information, None, 0, ctypes.byref(needed))
            self.assertGreater(needed.value, 0)
            buffer = ctypes.create_string_buffer(needed.value)
            self.assertTrue(
                get_security(
                    str(path),
                    information,
                    buffer,
                    needed.value,
                    ctypes.byref(needed),
                )
            )
            return bytes(buffer.raw[: needed.value])

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "managed.py"
            target.write_bytes(b"previous\n")
            stream = Path(str(target) + ":reqsys-test-stream")
            stream.write_bytes(b"preserve-stream\n")
            before = security_descriptor(target)
            updater.bootstrap.atomic_write(target, b"desired\n")
            self.assertEqual(target.read_bytes(), b"desired\n")
            self.assertEqual(stream.read_bytes(), b"preserve-stream\n")
            self.assertEqual(security_descriptor(target), before)

    def test_failure_after_each_managed_copy_restores_exact_legacy_state(self):
        commit = "f" * 40
        for failed_destination in updater.bootstrap.RUNTIME_MAP.values():
            with self.subTest(destination=failed_destination):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    install = root / "install"
                    work_root = root / "workers"
                    bundle = self.make_current_bundle(root)
                    previous, unmanaged, unmanaged_mode = self.seed_legacy_runtime(install)
                    (install / "update-receipt.json").write_text(
                        json.dumps(
                            {
                                "result": "HOST_UPDATE_OK",
                                "source_commit": "0" * 40,
                            }
                        ),
                        encoding="utf-8",
                    )
                    real_atomic = updater.bootstrap.atomic_write
                    snapshot_seen = False

                    def fail_after_target(path: Path, payload: bytes) -> None:
                        nonlocal snapshot_seen
                        real_atomic(path, payload)
                        if path.resolve() == (
                            install / failed_destination
                        ).resolve():
                            manifests = list(
                                install.glob(
                                    "backup-*/rollback-manifest.json"
                                )
                            )
                            snapshot_seen = len(manifests) == 1
                            raise OSError(
                                f"injected-copy-failure:{failed_destination}"
                            )

                    argv = self.update_argv(install, work_root, commit)
                    with mock.patch.object(
                        updater.bootstrap,
                        "download_bundle",
                        side_effect=self.copy_bundle(bundle, commit),
                    ), mock.patch.object(
                        updater.bootstrap,
                        "atomic_write",
                        side_effect=fail_after_target,
                    ), mock.patch.object(
                        updater,
                        "restore_backup",
                        wraps=updater.restore_backup,
                    ) as restore_mock, mock.patch.object(sys, "argv", argv):
                        self.assertEqual(
                            updater.main(), updater.EXIT_HOST_UPDATE
                        )

                    self.assertEqual(restore_mock.call_count, 1)

                    self.assertTrue(snapshot_seen)
                    self.assert_legacy_state(
                        install,
                        previous,
                        unmanaged,
                        unmanaged_mode,
                    )
                    receipt = json.loads(
                        (install / "update-receipt.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    self.assertEqual(
                        receipt["result"], "HOST_UPDATE_BLOCKED"
                    )
                    self.assertNotEqual(
                        receipt["result"], "HOST_UPDATE_OK"
                    )
                    self.assertTrue(receipt["rollback_attempted"])
                    self.assertTrue(receipt["rollback_succeeded"])
                    self.assertIsNone(receipt["rollback_error"])
                    self.assertTrue(receipt["backup_dir"])
                    rollback_manifest = (
                        Path(receipt["backup_dir"])
                        / "rollback-manifest.json"
                    )
                    snapshot = json.loads(
                        rollback_manifest.read_text(encoding="utf-8")
                    )
                    self.assertEqual(
                        set(snapshot["entries"]),
                        set(updater.bootstrap.RUNTIME_MAP.values()),
                    )

    def test_post_copy_validation_failure_also_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = root / "install"
            work_root = root / "workers"
            bundle = self.make_current_bundle(root)
            previous, unmanaged, unmanaged_mode = self.seed_legacy_runtime(install)
            commit = "e" * 40
            argv = self.update_argv(install, work_root, commit)
            with mock.patch.object(
                updater.bootstrap,
                "download_bundle",
                side_effect=self.copy_bundle(bundle, commit),
            ), mock.patch.object(
                updater.bootstrap,
                "resolve_git",
                side_effect=OSError("injected-post-copy-failure"),
            ), mock.patch.object(sys, "argv", argv):
                self.assertEqual(
                    updater.main(), updater.EXIT_HOST_UPDATE
                )
            self.assert_legacy_state(
                install,
                previous,
                unmanaged,
                unmanaged_mode,
            )
            receipt = json.loads(
                (install / "update-receipt.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(receipt["result"], "HOST_UPDATE_BLOCKED")
            self.assertTrue(receipt["rollback_succeeded"])

    def test_rollback_failure_is_not_reported_as_success_and_replay_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = root / "install"
            work_root = root / "workers"
            bundle = self.make_current_bundle(root)
            self.seed_legacy_runtime(install)
            commit = "4" * 40
            argv = self.update_argv(install, work_root, commit)
            failed_destination = list(updater.bootstrap.RUNTIME_MAP.values())[1]
            real_atomic = updater.bootstrap.atomic_write

            def fail_after_second_target(path: Path, payload: bytes) -> None:
                real_atomic(path, payload)
                if path.resolve() == (install / failed_destination).resolve():
                    raise OSError("injected-second-file-failure")

            with mock.patch.object(
                updater.bootstrap,
                "download_bundle",
                side_effect=self.copy_bundle(bundle, commit),
            ), mock.patch.object(
                updater.bootstrap,
                "atomic_write",
                side_effect=fail_after_second_target,
            ), mock.patch.object(
                updater,
                "restore_backup",
                side_effect=OSError("injected-rollback-failure"),
            ), mock.patch.object(sys, "argv", argv):
                self.assertEqual(updater.main(), updater.EXIT_HOST_UPDATE)

            blocked = json.loads(
                (install / "update-receipt.json").read_text(encoding="utf-8")
            )
            self.assertTrue(blocked["rollback_attempted"])
            self.assertFalse(blocked["rollback_succeeded"])
            self.assertIn("injected-rollback-failure", blocked["rollback_error"])
            self.assertEqual(
                blocked["reconciliation"]["overall"],
                "mixed_or_divergent",
            )

            with mock.patch.object(
                updater.bootstrap,
                "download_bundle",
                side_effect=self.copy_bundle(bundle, commit),
            ), mock.patch.object(
                updater.bootstrap,
                "resolve_git",
                return_value=(Path(sys.executable), "test"),
            ), mock.patch.object(
                updater.bootstrap,
                "prepare_validation_repo",
                return_value=(work_root / "validation", commit),
            ), mock.patch.object(sys, "argv", argv):
                self.assertEqual(updater.main(), 0)

            recovered = json.loads(
                (install / "update-receipt.json").read_text(encoding="utf-8")
            )
            self.assertEqual(recovered["result"], "HOST_UPDATE_OK")
            self.assertTrue(recovered["reconciliation"]["desired_state"])

    def test_same_commit_replay_performs_readback_without_reinstall(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = root / "install"
            work_root = root / "workers"
            bundle = self.make_current_bundle(root)
            self.seed_legacy_runtime(install)
            commit = "5" * 40
            argv = self.update_argv(install, work_root, commit)
            common = [
                mock.patch.object(
                    updater.bootstrap,
                    "download_bundle",
                    side_effect=self.copy_bundle(bundle, commit),
                ),
                mock.patch.object(
                    updater.bootstrap,
                    "resolve_git",
                    return_value=(Path(sys.executable), "test"),
                ),
                mock.patch.object(
                    updater.bootstrap,
                    "prepare_validation_repo",
                    return_value=(work_root / "validation", commit),
                ),
                mock.patch.object(sys, "argv", argv),
            ]
            with common[0], common[1], common[2], common[3]:
                self.assertEqual(updater.main(), 0)
            first = json.loads(
                (install / "update-receipt.json").read_text(encoding="utf-8")
            )

            with mock.patch.object(
                updater.bootstrap,
                "download_bundle",
                side_effect=self.copy_bundle(bundle, commit),
            ), mock.patch.object(
                updater.bootstrap,
                "resolve_git",
                return_value=(Path(sys.executable), "test"),
            ), mock.patch.object(
                updater.bootstrap,
                "prepare_validation_repo",
                return_value=(work_root / "validation", commit),
            ), mock.patch.object(
                updater.bootstrap,
                "prepare_runtime_rollback",
                side_effect=AssertionError("replay criou snapshot"),
            ), mock.patch.object(
                updater.bootstrap,
                "install_bundle",
                side_effect=AssertionError("replay reinstalou runtime"),
            ), mock.patch.object(sys, "argv", argv):
                self.assertEqual(updater.main(), 0)

            replay = json.loads(
                (install / "update-receipt.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                replay["result"], "HOST_UPDATE_ALREADY_CURRENT_VERIFIED"
            )
            self.assertFalse(replay["mutation_started"])
            self.assertNotEqual(first["transaction_id"], replay["transaction_id"])
            self.assertFalse((install / "install-receipt.json").exists())
            self.assertFalse((install / "update-transaction.json").exists())

    def test_pending_journal_is_reconciled_before_stale_success_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = root / "install"
            work_root = root / "workers"
            bundle = self.make_current_bundle(root)
            self.seed_legacy_runtime(install)
            commit = "6" * 40
            backup = updater.bootstrap.prepare_runtime_rollback(bundle, install)
            updater._durable_atomic_json(
                install / "update-transaction.json",
                {
                    "version": 1,
                    "transaction_id": "interrupted-transaction",
                    "source_commit": commit,
                    "backup_dir": str(backup),
                    "prepared_at": updater.utc_now(),
                },
            )
            source, destination = next(iter(updater.bootstrap.RUNTIME_MAP.items()))
            updater.bootstrap.atomic_write(
                install / destination, (bundle / source).read_bytes()
            )
            (install / "update-receipt.json").write_text(
                json.dumps(
                    {"result": "HOST_UPDATE_OK", "source_commit": commit}
                ),
                encoding="utf-8",
            )
            argv = self.update_argv(install, work_root, commit)
            with mock.patch.object(
                updater.bootstrap,
                "download_bundle",
                side_effect=self.copy_bundle(bundle, commit),
            ), mock.patch.object(
                updater.bootstrap,
                "resolve_git",
                return_value=(Path(sys.executable), "test"),
            ), mock.patch.object(
                updater.bootstrap,
                "prepare_validation_repo",
                return_value=(work_root / "validation", commit),
            ), mock.patch.object(sys, "argv", argv):
                self.assertEqual(updater.main(), 0)

            receipt = json.loads(
                (install / "update-receipt.json").read_text(encoding="utf-8")
            )
            self.assertEqual(receipt["result"], "HOST_UPDATE_OK")
            self.assertTrue(receipt["reconciliation"]["desired_state"])
            self.assertFalse((install / "update-transaction.json").exists())

    def test_concurrent_updater_writes_only_unique_blocked_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = root / "install"
            work_root = root / "workers"
            self.seed_legacy_runtime(install)
            commit = "7" * 40
            held = updater.acquire_update_lock(install)
            try:
                argv = self.update_argv(install, work_root, commit)
                with mock.patch.object(sys, "argv", argv):
                    self.assertEqual(
                        updater.main(), updater.EXIT_HOST_UPDATE
                    )
                self.assertFalse((install / "update-receipt.json").exists())
                fallbacks = list(install.glob("update-recovery-*.json"))
                self.assertEqual(len(fallbacks), 1)
                blocked = json.loads(fallbacks[0].read_text(encoding="utf-8"))
                self.assertEqual(blocked["phase"], "acquire_update_lock")
                self.assertIn(
                    "lock da transação não adquirido",
                    blocked["primary_receipt_error"],
                )
            finally:
                held.close()

    def test_rollback_refuses_to_remove_divergent_new_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = root / "install"
            bundle = self.make_current_bundle(root)
            self.seed_legacy_runtime(install)
            backup = updater.bootstrap.prepare_runtime_rollback(
                bundle, install
            )
            runner = install / "bin" / "runner_version_preflight.py"
            runner.parent.mkdir(parents=True, exist_ok=True)
            runner.write_bytes(b"created-by-someone-else\n")
            with self.assertRaisesRegex(
                updater.bootstrap.HostBootstrapError,
                "divergente não será removido",
            ):
                updater.restore_backup(install, backup)
            self.assertEqual(
                runner.read_bytes(), b"created-by-someone-else\n"
            )

    def test_rollback_refuses_to_overwrite_divergent_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = root / "install"
            bundle = self.make_current_bundle(root)
            self.seed_legacy_runtime(install)
            backup = updater.bootstrap.prepare_runtime_rollback(
                bundle, install
            )
            gateway = install / "bin" / "command_gateway.py"
            gateway.write_bytes(b"concurrent-third-state\n")
            with self.assertRaisesRegex(
                updater.bootstrap.HostBootstrapError,
                "existente divergente não será sobrescrito",
            ):
                updater.restore_backup(install, backup)
            self.assertEqual(gateway.read_bytes(), b"concurrent-third-state\n")

    def test_rollback_replace_failure_cleans_owned_temporary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = root / "install"
            bundle = self.make_current_bundle(root)
            self.seed_legacy_runtime(install)
            backup = updater.bootstrap.prepare_runtime_rollback(bundle, install)
            gateway = install / "bin" / "command_gateway.py"
            gateway.write_bytes((bundle / "scripts" / "command_gateway.py").read_bytes())

            with mock.patch.object(
                updater.bootstrap,
                "replace_file",
                side_effect=PermissionError("injected rollback sharing violation"),
            ), self.assertRaisesRegex(PermissionError, "sharing violation"):
                updater.restore_backup(install, backup)

            self.assertEqual(list(gateway.parent.glob("command_gateway.py.rollback-*")), [])

    def test_legacy_bundle_upgrade_starts_launcher_and_keeps_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = root / "install"
            work_root = root / "workers"
            bundle = self.make_current_bundle(root)
            previous, unmanaged, unmanaged_mode = self.seed_legacy_runtime(install)
            commit = "d" * 40
            argv = self.update_argv(install, work_root, commit)
            with mock.patch.object(
                updater.bootstrap,
                "download_bundle",
                side_effect=self.copy_bundle(bundle, commit),
            ), mock.patch.object(
                updater.bootstrap,
                "resolve_git",
                return_value=(Path(sys.executable), "test"),
            ), mock.patch.object(
                updater.bootstrap,
                "prepare_validation_repo",
                return_value=(work_root / "validation", commit),
            ), mock.patch.object(sys, "argv", argv):
                self.assertEqual(updater.main(), 0)

            runner_module = (
                install / "bin" / "runner_version_preflight.py"
            )
            self.assertTrue(runner_module.is_file())
            self.assertEqual(unmanaged.read_bytes(), b"preserve-me\n")
            receipt = json.loads(
                (install / "update-receipt.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(receipt["result"], "HOST_UPDATE_OK")
            self.assertEqual(receipt["source_commit"], commit)
            backup = Path(receipt["backup_dir"])
            self.assertTrue(
                (backup / "rollback-manifest.json").is_file()
            )
            self.assertFalse(
                (
                    backup
                    / "files"
                    / "bin"
                    / "runner_version_preflight.py"
                ).exists()
            )
            for destination, (payload, mode) in previous.items():
                saved = backup / "files" / destination
                self.assertEqual(saved.read_bytes(), payload)
                self.assertEqual(
                    stat.S_IMODE(saved.stat().st_mode), mode
                )

            outside_source = root / "outside-source-tree"
            outside_source.mkdir()
            env = os.environ.copy()
            env.pop("PYTHONPATH", None)
            env["PYTHONNOUSERSITE"] = "1"
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(install / "bin" / "session_launcher.py"),
                    "--help",
                ],
                cwd=outside_source,
                env=env,
                text=True,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=30,
            )
            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout={result.stdout}\nstderr={result.stderr}",
            )
            self.assertNotIn("ModuleNotFoundError", result.stderr)


if __name__ == "__main__":
    unittest.main()

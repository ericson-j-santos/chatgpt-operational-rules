from __future__ import annotations

import hashlib
import contextlib
import io
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
import noteri_gateway_legacy_v1_rebaseline as legacy


class HostRuntimeUpdateTests(unittest.TestCase):
    def test_legacy_v1_verifier_uses_original_sources_then_restores_profile(self) -> None:
        selected = updater.runtime_map_for_profile(True, platform="nt", host="Noteri")
        observed: list[dict[str, str]] = []

        def verify(*_args, **_kwargs):
            observed.append(dict(updater.bootstrap.RUNTIME_MAP))
            return {"verified": True}

        with mock.patch.object(updater.bootstrap, "RUNTIME_MAP", selected), \
                mock.patch.object(legacy, "verify_legacy_v1_current_state",
                                  side_effect=verify):
            self.assertEqual(updater.verify_legacy_v1_with_original_map(
                Path("unused"), receipt_sha256="a" * 64,
                manifest_sha256="b" * 64), {"verified": True})
            self.assertEqual(updater.bootstrap.RUNTIME_MAP, selected)
        self.assertEqual(observed, [updater.DEFAULT_RUNTIME_MAP])

        with mock.patch.object(updater.bootstrap, "RUNTIME_MAP", selected), \
                mock.patch.object(legacy, "verify_legacy_v1_current_state",
                                  side_effect=RuntimeError("injected")):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                updater.verify_legacy_v1_with_original_map(
                    Path("unused"), receipt_sha256="a" * 64,
                    manifest_sha256="b" * 64)
            self.assertEqual(updater.bootstrap.RUNTIME_MAP, selected)

    def test_noteri_wsl_profile_is_explicit_and_host_bound(self) -> None:
        default = updater.runtime_map_for_profile(False, platform="nt", host="Noteri")
        self.assertEqual(default, updater.DEFAULT_RUNTIME_MAP)
        self.assertNotIn(updater.NOTERI_WSL_POLICY_SOURCE, default)
        selected = updater.runtime_map_for_profile(True, platform="nt", host="Noteri")
        self.assertNotIn(updater.WINDOWS_POLICY_SOURCE, selected)
        self.assertEqual(selected[updater.NOTERI_WSL_POLICY_SOURCE], "config/policy.json")
        for platform, host in (("posix", "Noteri"), ("nt", "Other"),
                               ("nt", "Noteri-renamed")):
            with self.subTest(platform=platform, host=host):
                with self.assertRaises(updater.bootstrap.HostBootstrapError):
                    updater.runtime_map_for_profile(True, platform=platform, host=host)

    def test_noteri_profile_uses_verified_bundle_not_generated_policy(self) -> None:
        selected = updater.runtime_map_for_profile(True, platform="nt", host="Noteri")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            updater.bootstrap, "RUNTIME_MAP", selected
        ):
            bundle = self.make_current_bundle(Path(tmp))
            _, index = updater.bootstrap.verify_bundle(bundle)
            self.assertIn(updater.NOTERI_WSL_POLICY_SOURCE, index)
            self.assertNotIn(updater.WINDOWS_POLICY_SOURCE, selected)
            policy = json.loads((bundle / updater.NOTERI_WSL_POLICY_SOURCE).read_text())
            self.assertIs(policy["noteri_wsl_running_inventory"], True)
            self.assertIn("wsl", policy["blocked_executables"])

    def test_noteri_profile_update_and_replay_preserve_unmanaged_file(self) -> None:
        selected = updater.runtime_map_for_profile(True, platform="nt", host="Noteri")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install, work_root = root / "install", root / "workers"
            with mock.patch.object(updater.bootstrap, "RUNTIME_MAP", selected):
                bundle = self.make_current_bundle(root)
                _, sentinel, _ = self.seed_legacy_runtime(install)
            commit = "a" * 40
            argv = self.update_argv(install, work_root, commit) + [
                "--noteri-wsl-inventory-profile"
            ]
            common = [
                mock.patch.object(updater, "runtime_map_for_profile", return_value=selected),
                mock.patch.object(updater.bootstrap, "download_bundle",
                                  side_effect=self.copy_bundle(bundle, commit)),
                mock.patch.object(updater.bootstrap, "resolve_git",
                                  return_value=(Path(sys.executable), "test")),
                mock.patch.object(updater.bootstrap, "prepare_validation_repo",
                                  return_value=(work_root / "validation", commit)),
                mock.patch.object(sys, "argv", argv),
            ]
            with common[0], common[1], common[2], common[3], common[4], \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(updater.main(), 0)
            self.assertEqual(sentinel.read_bytes(), b"preserve-me\n")
            policy_target = install / "config" / "policy.json"
            self.assertEqual(policy_target.read_bytes(),
                             (bundle / updater.NOTERI_WSL_POLICY_SOURCE).read_bytes())
            first = json.loads((install / "update-receipt.json").read_text())
            self.assertTrue(first["noteri_wsl_inventory_profile"])
            with common[0], common[1], common[2], common[3], common[4], \
                    mock.patch.object(updater.bootstrap, "install_bundle",
                                      side_effect=AssertionError("replay reinstalou")), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(updater.main(), 0)
            replay = json.loads((install / "update-receipt.json").read_text())
            self.assertEqual(replay["result"], "HOST_UPDATE_ALREADY_CURRENT_VERIFIED")
            self.assertFalse(replay["mutation_started"])
            self.assertEqual(sentinel.read_bytes(), b"preserve-me\n")

    def test_noteri_profile_failure_restores_previous_runtime(self) -> None:
        selected = updater.runtime_map_for_profile(True, platform="nt", host="Noteri")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install, work_root = root / "install", root / "workers"
            with mock.patch.object(updater.bootstrap, "RUNTIME_MAP", selected):
                bundle = self.make_current_bundle(root)
                previous, sentinel, sentinel_mode = self.seed_legacy_runtime(install)
            commit = "b" * 40
            argv = self.update_argv(install, work_root, commit) + [
                "--noteri-wsl-inventory-profile"
            ]
            real_atomic = updater.bootstrap.atomic_write

            def fail_after_policy(path: Path, payload: bytes) -> None:
                real_atomic(path, payload)
                if path == install / "config" / "policy.json":
                    raise OSError("injected-profile-write-failure")

            with mock.patch.object(updater, "runtime_map_for_profile", return_value=selected), \
                    mock.patch.object(updater.bootstrap, "download_bundle",
                                      side_effect=self.copy_bundle(bundle, commit)), \
                    mock.patch.object(updater.bootstrap, "atomic_write",
                                      side_effect=fail_after_policy), \
                    mock.patch.object(sys, "argv", argv), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(updater.main(), updater.EXIT_HOST_UPDATE)
            self.assert_legacy_state(install, previous, sentinel, sentinel_mode)
            receipt = json.loads((install / "update-receipt.json").read_text())
            self.assertEqual(receipt["result"], "HOST_UPDATE_BLOCKED")
            self.assertTrue(receipt["rollback_succeeded"])

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

    def test_windows_staged_replace_clone_failure_keeps_target_and_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "managed.py"
            source = root / "managed.py.tmp-owned"
            target.write_bytes(b"previous\n")
            source.write_bytes(b"desired\n")
            with mock.patch.object(
                updater.bootstrap,
                "_windows_get_security_descriptor",
                return_value=b"descriptor",
            ), mock.patch.object(
                updater.bootstrap,
                "_windows_security_fingerprint",
                return_value={"same": True},
            ), mock.patch.multiple(
                updater.bootstrap,
                _windows_get_file_attributes=mock.Mock(return_value=0x20),
                _windows_set_file_attributes=mock.Mock(),
                _windows_file_identity=mock.Mock(return_value=(1, 1)),
                _windows_stream_fingerprint=mock.Mock(return_value=()),
                _windows_owner_group_equivalent=mock.Mock(return_value=True),
            ), mock.patch.object(
                updater.bootstrap,
                "_windows_copy_file",
                side_effect=PermissionError("injected clone failure"),
            ), mock.patch.object(
                updater.bootstrap, "_windows_move_file"
            ) as move_file, mock.patch.object(
                updater.bootstrap, "_cleanup_owned_stage"
            ) as cleanup:
                with self.assertRaisesRegex(PermissionError, "clone failure"):
                    updater.bootstrap._windows_replace_file(source, target)
            self.assertEqual(target.read_bytes(), b"previous\n")
            self.assertEqual(source.read_bytes(), b"desired\n")
            move_file.assert_not_called()
            cleanup.assert_not_called()

    def test_windows_staged_replace_security_failure_cleans_owned_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "managed.py"
            source = root / "managed.py.tmp-owned"
            target.write_bytes(b"previous\n")
            source.write_bytes(b"desired\n")

            def clone(observed: Path, stage: Path) -> None:
                shutil.copyfile(observed, stage)

            with mock.patch.object(
                updater.bootstrap,
                "_windows_get_security_descriptor",
                return_value=b"descriptor",
            ), mock.patch.object(
                updater.bootstrap,
                "_windows_security_fingerprint",
                return_value={"same": True},
            ), mock.patch.multiple(
                updater.bootstrap,
                _windows_get_file_attributes=mock.Mock(return_value=0x20),
                _windows_set_file_attributes=mock.Mock(),
                _windows_file_identity=mock.Mock(return_value=(1, 1)),
                _windows_stream_fingerprint=mock.Mock(return_value=()),
                _windows_owner_group_equivalent=mock.Mock(return_value=True),
            ), mock.patch.object(
                updater.bootstrap,
                "_windows_security_equivalent",
                return_value=False,
            ), mock.patch.object(
                updater.bootstrap,
                "_windows_copy_file",
                side_effect=clone,
            ), mock.patch.object(
                updater.bootstrap,
                "_windows_set_dacl",
                side_effect=PermissionError("injected security failure"),
            ), mock.patch.object(
                updater.bootstrap, "_windows_move_file"
            ) as move_file:
                with self.assertRaisesRegex(PermissionError, "security failure"):
                    updater.bootstrap._windows_replace_file(source, target)
            self.assertEqual(target.read_bytes(), b"previous\n")
            self.assertEqual(source.read_bytes(), b"desired\n")
            self.assertEqual(list(root.glob("managed.py.replace-stage-*")), [])
            move_file.assert_not_called()

    def test_windows_staged_replace_move_failure_is_pre_activation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "managed.py"
            source = root / "managed.py.tmp-owned"
            target.write_bytes(b"previous\n")
            source.write_bytes(b"desired\n")

            def clone(observed: Path, stage: Path) -> None:
                shutil.copyfile(observed, stage)

            with mock.patch.object(
                updater.bootstrap,
                "_windows_get_security_descriptor",
                return_value=b"descriptor",
            ), mock.patch.object(
                updater.bootstrap,
                "_windows_security_fingerprint",
                return_value={"same": True},
            ), mock.patch.multiple(
                updater.bootstrap,
                _windows_get_file_attributes=mock.Mock(return_value=0x20),
                _windows_set_file_attributes=mock.Mock(),
                _windows_file_identity=mock.Mock(return_value=(1, 1)),
                _windows_stream_fingerprint=mock.Mock(return_value=()),
                _windows_owner_group_equivalent=mock.Mock(return_value=True),
            ), mock.patch.object(
                updater.bootstrap,
                "_windows_security_equivalent",
                return_value=True,
            ), mock.patch.object(
                updater.bootstrap,
                "_windows_copy_file",
                side_effect=clone,
            ), mock.patch.object(
                updater.bootstrap, "_windows_set_dacl"
            ), mock.patch.object(
                updater.bootstrap,
                "_windows_move_file",
                side_effect=PermissionError("injected sharing violation"),
            ):
                with self.assertRaisesRegex(PermissionError, "sharing violation"):
                    updater.bootstrap._windows_replace_file(source, target)
            self.assertEqual(target.read_bytes(), b"previous\n")
            self.assertFalse(source.exists())
            self.assertEqual(list(root.glob("managed.py.replace-stage-*")), [])

    def test_windows_staged_replace_reports_cleanup_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "managed.py"
            source = root / "managed.py.tmp-owned"
            target.write_bytes(b"previous\n")
            source.write_bytes(b"desired\n")

            def clone(observed: Path, stage: Path) -> None:
                shutil.copyfile(observed, stage)

            with mock.patch.object(
                updater.bootstrap,
                "_windows_get_security_descriptor",
                return_value=b"descriptor",
            ), mock.patch.object(
                updater.bootstrap,
                "_windows_security_fingerprint",
                return_value={"same": True},
            ), mock.patch.multiple(
                updater.bootstrap,
                _windows_get_file_attributes=mock.Mock(return_value=0x20),
                _windows_set_file_attributes=mock.Mock(),
                _windows_file_identity=mock.Mock(return_value=(1, 1)),
                _windows_stream_fingerprint=mock.Mock(return_value=()),
                _windows_owner_group_equivalent=mock.Mock(return_value=True),
            ), mock.patch.object(
                updater.bootstrap,
                "_windows_security_equivalent",
                return_value=False,
            ), mock.patch.object(
                updater.bootstrap,
                "_windows_copy_file",
                side_effect=clone,
            ), mock.patch.object(
                updater.bootstrap,
                "_windows_set_dacl",
                side_effect=PermissionError("injected security failure"),
            ), mock.patch.object(
                updater.bootstrap,
                "_cleanup_owned_stage",
                side_effect=PermissionError("injected cleanup failure"),
            ):
                with self.assertRaisesRegex(
                    OSError, "security failure.*limpeza.*cleanup failure"
                ):
                    updater.bootstrap._windows_replace_file(source, target)
            self.assertEqual(target.read_bytes(), b"previous\n")
            self.assertEqual(source.read_bytes(), b"desired\n")
            self.assertEqual(len(list(root.glob("managed.py.replace-stage-*"))), 1)

    @unittest.skipUnless(os.name == "nt", "requer APIs de arquivo do Windows")
    def test_windows_replace_preserves_security_descriptor_and_ads(self):
        import ctypes
        from ctypes import wintypes

        self.maxDiff = None

        class Acl(ctypes.Structure):
            _fields_ = [
                ("revision", wintypes.BYTE),
                ("reserved_1", wintypes.BYTE),
                ("size", wintypes.WORD),
                ("ace_count", wintypes.WORD),
                ("reserved_2", wintypes.WORD),
            ]

        class AceHeader(ctypes.Structure):
            _fields_ = [
                ("type", wintypes.BYTE),
                ("flags", wintypes.BYTE),
                ("size", wintypes.WORD),
            ]

        security = ctypes.WinDLL("advapi32", use_last_error=True)
        get_security = security.GetFileSecurityW
        get_security.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        get_security.restype = wintypes.BOOL
        get_owner = security.GetSecurityDescriptorOwner
        get_owner.argtypes = [
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.BOOL),
        ]
        get_owner.restype = wintypes.BOOL
        get_group = security.GetSecurityDescriptorGroup
        get_group.argtypes = get_owner.argtypes
        get_group.restype = wintypes.BOOL
        get_dacl = security.GetSecurityDescriptorDacl
        get_dacl.argtypes = [
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.BOOL),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.BOOL),
        ]
        get_dacl.restype = wintypes.BOOL
        get_control = security.GetSecurityDescriptorControl
        get_control.argtypes = [
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.WORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        get_control.restype = wintypes.BOOL
        get_length_sid = security.GetLengthSid
        get_length_sid.argtypes = [wintypes.LPVOID]
        get_length_sid.restype = wintypes.DWORD
        is_valid_sid = security.IsValidSid
        is_valid_sid.argtypes = [wintypes.LPVOID]
        is_valid_sid.restype = wintypes.BOOL
        get_ace = security.GetAce
        get_ace.argtypes = [
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.LPVOID),
        ]
        get_ace.restype = wintypes.BOOL
        convert_sddl = security.ConvertStringSecurityDescriptorToSecurityDescriptorW
        convert_sddl.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.DWORD),
        ]
        convert_sddl.restype = wintypes.BOOL
        set_named_security = security.SetNamedSecurityInfoW
        set_named_security.argtypes = [
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.LPVOID,
        ]
        set_named_security.restype = wintypes.DWORD
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        local_free = kernel32.LocalFree
        local_free.argtypes = [wintypes.LPVOID]
        local_free.restype = wintypes.LPVOID

        def apply_protected_deny_allow_dacl(path: Path) -> None:
            descriptor = wintypes.LPVOID()
            descriptor_size = wintypes.DWORD()
            self.assertTrue(
                convert_sddl(
                    "D:P(D;;FR;;;AN)(A;;FA;;;WD)",
                    1,
                    ctypes.byref(descriptor),
                    ctypes.byref(descriptor_size),
                )
            )
            try:
                present = wintypes.BOOL()
                dacl = wintypes.LPVOID()
                defaulted = wintypes.BOOL()
                self.assertTrue(
                    get_dacl(
                        descriptor,
                        ctypes.byref(present),
                        ctypes.byref(dacl),
                        ctypes.byref(defaulted),
                    )
                )
                self.assertTrue(present.value)
                self.assertTrue(dacl.value)
                result = set_named_security(
                    str(path),
                    1,
                    0x00000004 | 0x80000000,
                    None,
                    None,
                    dacl,
                    None,
                )
                if result:
                    raise ctypes.WinError(result)
            finally:
                self.assertFalse(local_free(descriptor))

        def sid_bytes(pointer: wintypes.LPVOID) -> bytes | None:
            if not pointer.value:
                return None
            self.assertTrue(is_valid_sid(pointer))
            length = get_length_sid(pointer)
            self.assertGreater(length, 0)
            return ctypes.string_at(pointer, length)

        def ace_state(pointer: wintypes.LPVOID) -> dict[str, object]:
            header = AceHeader.from_address(pointer.value)
            self.assertGreaterEqual(header.size, 8)
            raw = ctypes.string_at(pointer, header.size)

            basic_sid_types = {
                0x00,  # ACCESS_ALLOWED_ACE_TYPE
                0x01,  # ACCESS_DENIED_ACE_TYPE
                0x02,  # SYSTEM_AUDIT_ACE_TYPE
                0x03,  # SYSTEM_ALARM_ACE_TYPE
                0x09,  # ACCESS_ALLOWED_CALLBACK_ACE_TYPE
                0x0A,  # ACCESS_DENIED_CALLBACK_ACE_TYPE
                0x0D,  # SYSTEM_AUDIT_CALLBACK_ACE_TYPE
                0x0E,  # SYSTEM_ALARM_CALLBACK_ACE_TYPE
                0x11,  # SYSTEM_MANDATORY_LABEL_ACE_TYPE
                0x12,  # SYSTEM_RESOURCE_ATTRIBUTE_ACE_TYPE
                0x13,  # SYSTEM_SCOPED_POLICY_ID_ACE_TYPE
                0x14,  # SYSTEM_PROCESS_TRUST_LABEL_ACE_TYPE
                0x15,  # SYSTEM_ACCESS_FILTER_ACE_TYPE
            }
            object_sid_types = {
                0x05,  # ACCESS_ALLOWED_OBJECT_ACE_TYPE
                0x06,  # ACCESS_DENIED_OBJECT_ACE_TYPE
                0x07,  # SYSTEM_AUDIT_OBJECT_ACE_TYPE
                0x08,  # SYSTEM_ALARM_OBJECT_ACE_TYPE
                0x0B,  # ACCESS_ALLOWED_CALLBACK_OBJECT_ACE_TYPE
                0x0C,  # ACCESS_DENIED_CALLBACK_OBJECT_ACE_TYPE
                0x0F,  # SYSTEM_AUDIT_CALLBACK_OBJECT_ACE_TYPE
                0x10,  # SYSTEM_ALARM_CALLBACK_OBJECT_ACE_TYPE
            }
            if header.type in basic_sid_types:
                sid_offset = 8
            elif header.type in object_sid_types:
                self.assertGreaterEqual(header.size, 12)
                object_flags = int.from_bytes(raw[8:12], "little")
                self.assertFalse(object_flags & ~0x03)
                sid_offset = 12
                if object_flags & 0x01:
                    sid_offset += 16
                if object_flags & 0x02:
                    sid_offset += 16
            elif header.type == 0x04:  # ACCESS_ALLOWED_COMPOUND_ACE_TYPE
                sid_offset = 12
            else:
                self.fail(f"tipo de ACE não suportado pelo teste: {header.type}")

            self.assertLess(sid_offset, header.size)
            sid_pointer = wintypes.LPVOID(pointer.value + sid_offset)
            sid = sid_bytes(sid_pointer)
            self.assertIsNotNone(sid)
            sid_end = sid_offset + len(sid)
            self.assertLessEqual(sid_end, header.size)
            return {
                "type": header.type,
                "flags": header.flags,
                "size": header.size,
                "mask": int.from_bytes(raw[4:8], "little"),
                "opaque_before_sid": raw[8:sid_offset],
                "sid": sid,
                "opaque_tail": raw[sid_end:],
            }

        def security_state(path: Path) -> dict[str, object]:
            needed = wintypes.DWORD()
            information = 0x00000001 | 0x00000002 | 0x00000004
            ctypes.set_last_error(0)
            get_security(str(path), information, None, 0, ctypes.byref(needed))
            self.assertEqual(ctypes.get_last_error(), 122)
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

            owner = wintypes.LPVOID()
            owner_defaulted = wintypes.BOOL()
            self.assertTrue(
                get_owner(buffer, ctypes.byref(owner), ctypes.byref(owner_defaulted))
            )
            group = wintypes.LPVOID()
            group_defaulted = wintypes.BOOL()
            self.assertTrue(
                get_group(buffer, ctypes.byref(group), ctypes.byref(group_defaulted))
            )
            dacl_present = wintypes.BOOL()
            dacl = wintypes.LPVOID()
            dacl_defaulted = wintypes.BOOL()
            self.assertTrue(
                get_dacl(
                    buffer,
                    ctypes.byref(dacl_present),
                    ctypes.byref(dacl),
                    ctypes.byref(dacl_defaulted),
                )
            )
            control = wintypes.WORD()
            revision = wintypes.DWORD()
            self.assertTrue(
                get_control(buffer, ctypes.byref(control), ctypes.byref(revision))
            )

            acl_state = None
            if dacl.value:
                header = Acl.from_address(dacl.value)
                aces = []
                for index in range(header.ace_count):
                    ace = wintypes.LPVOID()
                    self.assertTrue(get_ace(dacl, index, ctypes.byref(ace)))
                    aces.append(ace_state(ace))
                acl_state = {
                    "revision": header.revision,
                    "ace_count": header.ace_count,
                    "aces": tuple(aces),
                }

            dacl_auto_inherited = 0x0400
            dacl_protected = 0x1000
            return {
                "owner_sid": sid_bytes(owner),
                "owner_defaulted": bool(owner_defaulted.value),
                "group_sid": sid_bytes(group),
                "group_defaulted": bool(group_defaulted.value),
                "dacl_present": bool(dacl_present.value),
                "dacl_null": bool(dacl_present.value and not dacl.value),
                "dacl_defaulted": bool(dacl_defaulted.value),
                "dacl_protected": bool(control.value & dacl_protected),
                "dacl_auto_inherited": bool(
                    control.value & dacl_auto_inherited
                ),
                "dacl": acl_state,
                "control": control.value,
                "revision": revision.value,
            }

        def assert_security_preserved(
            before: dict[str, object], after: dict[str, object]
        ) -> None:
            for component in (
                "owner_sid",
                "owner_defaulted",
                "group_sid",
                "group_defaulted",
                "dacl_present",
                "dacl_null",
                "dacl_defaulted",
                "dacl_protected",
                "dacl",
                "revision",
            ):
                self.assertEqual(after[component], before[component], component)

            # ReplaceFileW pode marcar uma DACL idêntica como auto-herdada.
            # O bit é bookkeeping e não participa do AccessCheck; qualquer
            # outra mudança de controle (inclusive DACL_PROTECTED) continua erro.
            dacl_auto_inherited = 0x0400
            before_control = int(before["control"])
            after_control = int(after["control"])
            self.assertEqual(
                after_control & ~dacl_auto_inherited,
                before_control & ~dacl_auto_inherited,
            )
            changed = before_control ^ after_control
            self.assertIn(changed, (0, dacl_auto_inherited))
            if changed:
                self.assertFalse(before_control & dacl_auto_inherited)
                self.assertTrue(after_control & dacl_auto_inherited)

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "managed.py"
            target.write_bytes(b"previous\n")
            apply_protected_deny_allow_dacl(target)
            stream = Path(str(target) + ":reqsys-test-stream")
            stream.write_bytes(b"preserve-stream\n")
            unicode_stream = Path(str(target) + ":continuidade-ç-漢字")
            unicode_stream.write_bytes(b"preserve-unicode-stream\n")
            before = security_state(target)
            self.assertTrue(before["dacl_protected"])
            self.assertEqual(before["dacl"]["ace_count"], 2)
            self.assertEqual(
                tuple(ace["type"] for ace in before["dacl"]["aces"]), (1, 0)
            )
            updater.bootstrap.atomic_write(target, b"desired\n")
            self.assertEqual(target.read_bytes(), b"desired\n")
            self.assertEqual(stream.read_bytes(), b"preserve-stream\n")
            self.assertEqual(
                unicode_stream.read_bytes(), b"preserve-unicode-stream\n"
            )
            after = security_state(target)
            assert_security_preserved(before, after)

            # Controle do próprio teste: perder a DACL ou alterar uma ACE deve
            # continuar sendo detectado apesar da normalização permitida acima.
            without_dacl = dict(after)
            without_dacl["dacl_present"] = False
            without_dacl["dacl_null"] = False
            without_dacl["dacl"] = None
            with self.assertRaises(AssertionError):
                assert_security_preserved(before, without_dacl)

            acl_state = after["dacl"]
            self.assertIsInstance(acl_state, dict)
            aces = list(acl_state["aces"])
            self.assertGreater(len(aces), 0)
            changed_ace = dict(aces[0])
            changed_ace["mask"] = int(changed_ace["mask"]) ^ 0x01
            aces[0] = changed_ace
            changed_acl = dict(acl_state)
            changed_acl["aces"] = tuple(aces)
            with_changed_ace = dict(after)
            with_changed_ace["dacl"] = changed_acl
            with self.assertRaises(AssertionError):
                assert_security_preserved(before, with_changed_ace)

            with_changed_protection = dict(after)
            with_changed_protection["dacl_protected"] = not bool(
                after["dacl_protected"]
            )
            with self.assertRaises(AssertionError):
                assert_security_preserved(before, with_changed_protection)

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
            ) as replace_mock, mock.patch.object(
                updater.bootstrap,
                "_windows_move_file",
                side_effect=PermissionError("injected rollback sharing violation"),
            ) as move_mock, self.assertRaisesRegex(
                PermissionError, "sharing violation"
            ):
                updater.restore_backup(install, backup)

            self.assertEqual(list(gateway.parent.glob("command_gateway.py.rollback-*")), [])
            self.assertEqual(replace_mock.call_count + move_mock.call_count, 1)

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


    def _sample_windows_state(
        self,
        *,
        ace_mask: int = 0x001F01FF,
        control: int = 0x8004,
        streams: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        sid = "010100000000000512000000"
        return {
            "version": 1,
            "attributes": 0x20,
            "security": {
                "owner_sid": sid,
                "owner_defaulted": False,
                "group_sid": sid,
                "group_defaulted": False,
                "dacl_present": True,
                "dacl_null": False,
                "dacl_defaulted": False,
                "dacl_protected": False,
                "dacl": [
                    2,
                    1,
                    [[0, 0, 20, ace_mask, "", sid, ""]],
                ],
                "control": control,
                "revision": 1,
            },
            "streams": streams or [],
        }

    def test_windows_metadata_comparison_is_structural_and_ads_sensitive(self):
        expected = self._sample_windows_state()
        normalized = self._sample_windows_state(control=0x8404)
        updater.bootstrap._validate_windows_persisted_state(expected, "managed.py")
        self.assertTrue(
            updater.bootstrap._windows_persisted_state_equivalent(
                expected, normalized
            )
        )

        changed_ace = self._sample_windows_state(ace_mask=0x00120089)
        self.assertFalse(
            updater.bootstrap._windows_persisted_state_equivalent(
                expected, changed_ace
            )
        )
        changed_ads = self._sample_windows_state(
            streams=[
                {
                    "name": ":evidência:$DATA",
                    "size": 1,
                    "sha256": hashlib.sha256(b"x").hexdigest(),
                }
            ]
        )
        self.assertFalse(
            updater.bootstrap._windows_persisted_state_equivalent(
                expected, changed_ads
            )
        )

    def test_windows_dacl_security_information_preserves_protection(self):
        self.assertEqual(
            updater.bootstrap._windows_dacl_security_information(
                {"dacl_protected": True}
            ),
            0x80000004,
        )
        self.assertEqual(
            updater.bootstrap._windows_dacl_security_information(
                {"dacl_protected": False}
            ),
            0x00000004,
        )
        with self.assertRaisesRegex(OSError, "proteção da DACL Windows inválido"):
            updater.bootstrap._windows_dacl_security_information(
                {"dacl_protected": 1}
            )

    def test_windows_security_diagnostic_hashes_sids(self):
        private_owner = b"private-owner-sid"
        private_group = b"private-group-sid"
        private_ace_sid = b"private-ace-sid"
        diagnostic = updater.bootstrap._windows_security_diagnostic(
            {
                "owner_sid": private_owner,
                "owner_defaulted": False,
                "group_sid": private_group,
                "group_defaulted": False,
                "dacl_present": True,
                "dacl_null": False,
                "dacl_defaulted": False,
                "dacl_protected": True,
                "dacl": (
                    2,
                    1,
                    ((0, 0, 20, 0x001F01FF, b"", private_ace_sid, b""),),
                ),
                "control": 0x9404,
                "revision": 1,
            }
        )
        rendered = repr(diagnostic)
        self.assertNotIn(private_owner.decode("ascii"), rendered)
        self.assertNotIn(private_group.decode("ascii"), rendered)
        self.assertNotIn(private_ace_sid.decode("ascii"), rendered)
        self.assertEqual(diagnostic["control"], "0x9404")
        self.assertEqual(diagnostic["dacl"]["ace_count"], 1)
        self.assertEqual(
            diagnostic["dacl"]["aces"][0]["sid_sha256"],
            hashlib.sha256(private_ace_sid).hexdigest(),
        )

    def test_windows_rejects_v1_rollback_while_posix_remains_compatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = self.make_current_bundle(root)
            install = root / "install"
            for destination in updater.bootstrap.RUNTIME_MAP.values():
                target = install / destination
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(f"legacy:{destination}".encode("utf-8"))
            backup = updater.bootstrap.prepare_runtime_rollback(bundle, install)
            manifest_path = backup / "rollback-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["version"] = 1
            manifest.pop("metadata_contract")
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True), encoding="utf-8"
            )

            with mock.patch.object(
                updater.bootstrap, "_windows_metadata_required", return_value=False
            ):
                loaded = updater.bootstrap._load_runtime_rollback(install, backup)
            self.assertEqual(loaded["version"], 1)
            with mock.patch.object(
                updater.bootstrap, "_windows_metadata_required", return_value=True
            ):
                with self.assertRaisesRegex(
                    updater.bootstrap.HostBootstrapError,
                    "v1 não comprova ACL/ADS",
                ):
                    updater.bootstrap._load_runtime_rollback(install, backup)

    def test_reconcile_rejects_desired_bytes_with_windows_metadata_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = Path(tmp) / "install"
            target = install / "bin" / "managed.py"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"desired")
            expected_state = self._sample_windows_state()
            changed_state = self._sample_windows_state(ace_mask=0x00120089)
            snapshot = {
                "entries": {
                    "bin/managed.py": {
                        "source": "scripts/managed.py",
                        "existed": True,
                        "previous_sha256": hashlib.sha256(b"previous").hexdigest(),
                        "previous_size": len(b"previous"),
                        "previous_mode": stat.S_IMODE(target.stat().st_mode),
                        "desired_sha256": hashlib.sha256(b"desired").hexdigest(),
                        "desired_size": len(b"desired"),
                        "previous_windows_state": expected_state,
                    }
                }
            }
            with (
                mock.patch.object(
                    updater.bootstrap,
                    "RUNTIME_MAP",
                    {"scripts/managed.py": "bin/managed.py"},
                ),
                mock.patch.object(
                    updater.bootstrap,
                    "_load_runtime_rollback",
                    return_value=snapshot,
                ),
                mock.patch.object(
                    updater.bootstrap,
                    "_windows_metadata_required",
                    return_value=True,
                ),
                mock.patch.object(
                    updater.bootstrap,
                    "_windows_persisted_file_state",
                    return_value=changed_state,
                ),
            ):
                result = updater.reconcile_runtime(install, install / "backup")
            self.assertFalse(result["desired_state"])
            self.assertEqual(
                result["entries"]["bin/managed.py"]["state"], "divergent"
            )

    def test_windows_idempotent_readback_requires_receipt_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "bundle"
            bundle.mkdir()
            install = root / "install"
            target = install / "bin" / "managed.py"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"desired")
            digest = hashlib.sha256(b"desired").hexdigest()
            expected_state = self._sample_windows_state()
            changed_state = self._sample_windows_state(
                streams=[
                    {"name": ":extra:$DATA", "size": 0, "sha256": hashlib.sha256(b"").hexdigest()}
                ]
            )
            with (
                mock.patch.object(
                    updater.bootstrap,
                    "RUNTIME_MAP",
                    {"scripts/managed.py": "bin/managed.py"},
                ),
                mock.patch.object(
                    updater.bootstrap,
                    "verify_bundle",
                    return_value=({}, {"scripts/managed.py": {"sha256": digest, "size": 7}}),
                ),
                mock.patch.object(
                    updater.bootstrap,
                    "_windows_metadata_required",
                    return_value=True,
                ),
                mock.patch.object(
                    updater.bootstrap,
                    "_windows_persisted_file_state",
                    return_value=changed_state,
                ),
            ):
                with self.assertRaisesRegex(
                    updater.bootstrap.HostBootstrapError, "metadados reconciliáveis"
                ):
                    updater.desired_runtime_state(bundle, install)
                result = updater.desired_runtime_state(
                    bundle,
                    install,
                    {"bin/managed.py": {"windows_state": expected_state}},
                )
            self.assertFalse(result["matched"])

    def test_windows_rollback_clones_backup_without_consuming_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = root / "install"
            target = install / "bin" / "managed.py"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"desired")
            backup = install / "backup-test"
            previous = backup / "files" / "bin" / "managed.py"
            previous.parent.mkdir(parents=True)
            previous.write_bytes(b"previous")
            expected_state = self._sample_windows_state()
            entry = {
                "source": "scripts/managed.py",
                "existed": True,
                "previous_sha256": hashlib.sha256(b"previous").hexdigest(),
                "previous_size": len(b"previous"),
                "previous_mode": stat.S_IMODE(previous.stat().st_mode),
                "desired_sha256": hashlib.sha256(b"desired").hexdigest(),
                "desired_size": len(b"desired"),
                "previous_windows_state": expected_state,
            }
            snapshot = {"entries": {"bin/managed.py": entry}}

            def clone(source: Path, destination: Path, **kwargs):
                self.assertEqual(source, previous.resolve())
                self.assertEqual(kwargs["expected_metadata"], expected_state)
                shutil.copy2(source, destination)
                return expected_state

            def move(source: Path, destination: Path):
                self.assertNotEqual(source, previous.resolve())
                os.replace(source, destination)

            def identity(path: Path):
                observed = path.stat()
                return observed.st_dev, observed.st_ino

            with (
                mock.patch.object(
                    updater.bootstrap,
                    "_load_runtime_rollback",
                    return_value=snapshot,
                ),
                mock.patch.object(
                    updater.bootstrap,
                    "_windows_metadata_required",
                    return_value=True,
                ),
                mock.patch.object(
                    updater.bootstrap,
                    "_windows_path_matches_persisted_state",
                    return_value=True,
                ),
                mock.patch.object(
                    updater.bootstrap, "_windows_clone_verified", side_effect=clone
                ) as clone_mock,
                mock.patch.object(
                    updater.bootstrap, "_windows_move_file", side_effect=move
                ) as move_mock,
                mock.patch.object(
                    updater.bootstrap, "_windows_file_identity", side_effect=identity
                ),
            ):
                updater.bootstrap.restore_runtime_rollback(install, backup)
            self.assertEqual(target.read_bytes(), b"previous")
            self.assertEqual(previous.read_bytes(), b"previous")
            self.assertEqual(clone_mock.call_count, 1)
            self.assertEqual(move_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()

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
                    ), mock.patch.object(sys, "argv", argv):
                        self.assertEqual(
                            updater.main(), updater.EXIT_HOST_UPDATE
                        )

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

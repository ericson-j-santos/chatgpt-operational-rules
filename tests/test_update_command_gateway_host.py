from __future__ import annotations

import hashlib
import json
import os
import shutil
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
    def test_restore_backup_restores_only_runtime_map_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = root / "install"
            backup = root / "backup"
            for dest in updater.bootstrap.RUNTIME_MAP.values():
                old = backup / dest
                old.parent.mkdir(parents=True, exist_ok=True)
                old.write_text("old\n", encoding="utf-8")
                current = install / dest
                current.parent.mkdir(parents=True, exist_ok=True)
                current.write_text("new\n", encoding="utf-8")
            updater.restore_backup(install, backup)
            for dest in updater.bootstrap.RUNTIME_MAP.values():
                self.assertEqual((install / dest).read_text(encoding="utf-8"), "old\n")

    def test_receipt_writer_is_atomic_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "receipt.json"
            updater.atomic_json(target, {"result": "HOST_UPDATE_OK", "secret": False})
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(payload["result"], "HOST_UPDATE_OK")
            self.assertFalse(payload["secret"])

    def test_sha256_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "x"
            target.write_bytes(b"abc")
            self.assertEqual(updater.sha256_file(target), hashlib.sha256(b"abc").hexdigest())

    def test_restore_without_backup_fails_closed_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = Path(tmp) / "install"
            updater.restore_backup(install, None)
            self.assertFalse(install.exists())


    def test_legacy_bundle_without_runner_is_upgraded_and_launcher_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = root / "install"
            work_root = root / "workers"
            bundle = root / "current-bundle"
            bundle.mkdir()
            shutil.copy2(ROOT / "MANIFEST.json", bundle / "MANIFEST.json")
            for source, dest in updater.bootstrap.RUNTIME_MAP.items():
                bundle_target = bundle / source
                bundle_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / source, bundle_target)
                if source != "scripts/runner_version_preflight.py":
                    installed_target = install / dest
                    installed_target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(ROOT / source, installed_target)

            self.assertFalse(
                (install / "bin" / "runner_version_preflight.py").exists()
            )
            commit = "f" * 40
            updater_hash = updater.sha256_file(Path(updater.__file__).resolve())

            def copy_verified_bundle(_commit: str, target: Path) -> None:
                self.assertEqual(_commit, commit)
                shutil.copytree(bundle, target, dirs_exist_ok=True)

            argv = [
                "update_command_gateway_host.py",
                "--commit",
                commit,
                "--expected-self-sha256",
                updater_hash,
                "--install-root",
                str(install),
                "--work-root",
                str(work_root),
            ]
            with mock.patch.object(
                updater.bootstrap,
                "download_bundle",
                side_effect=copy_verified_bundle,
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

            runner_module = install / "bin" / "runner_version_preflight.py"
            self.assertTrue(runner_module.is_file())
            receipt = json.loads(
                (install / "update-receipt.json").read_text(encoding="utf-8")
            )
            self.assertEqual(receipt["result"], "HOST_UPDATE_OK")
            self.assertEqual(receipt["source_commit"], commit)
            self.assertTrue(receipt["backup_dir"])
            self.assertFalse(
                (
                    Path(receipt["backup_dir"])
                    / "bin"
                    / "runner_version_preflight.py"
                ).exists()
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

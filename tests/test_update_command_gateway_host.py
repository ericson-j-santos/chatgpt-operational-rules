from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
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


if __name__ == "__main__":
    unittest.main()

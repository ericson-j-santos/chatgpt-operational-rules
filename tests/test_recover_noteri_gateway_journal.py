from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import recover_noteri_gateway_journal as recovery


class NoteriJournalRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "install"
        self.root.mkdir()
        (self.root / "update.lock").write_bytes(b"\0")
        self.transaction_id = "1" * 32
        self.incident = "2" * 40
        self.backup = self.root / "backup-v2"
        self.backup.mkdir()
        self.legacy_backup = self.root / "backup-v1"
        self.legacy_backup.mkdir()
        self.managed = {"source/a.py": "bin/a.py", "source/b.json": "config/b.json"}
        self.patches = [
            mock.patch.object(recovery.updater, "DEFAULT_RUNTIME_MAP", self.managed),
            mock.patch.object(recovery.updater.bootstrap, "RUNTIME_MAP", self.managed),
            mock.patch.object(recovery.bootstrap, "RUNTIME_MAP", self.managed),
            mock.patch.object(recovery.bootstrap, "ROLLBACK_MANIFEST_VERSION", 2),
            mock.patch.object(recovery, "INCIDENT_RULES_SHA", self.incident),
            mock.patch.object(recovery, "_verified_reconciliation", side_effect=self.reconciliation),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.seed()

    @staticmethod
    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def write_json(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    def reconciliation(self, _root: Path, _backup: Path) -> dict:
        return {
            "overall": "previous",
            "previous_state": True,
            "desired_state": False,
            "entries": {
                destination: {"state": "previous", "previous": True, "desired": False}
                for destination in self.managed.values()
            },
        }

    def seed(self) -> None:
        entries = {}
        for source, destination in self.managed.items():
            current = self.root / destination
            saved = self.backup / "files" / destination
            current.parent.mkdir(parents=True, exist_ok=True)
            saved.parent.mkdir(parents=True, exist_ok=True)
            current.write_bytes(("previous:" + destination).encode())
            saved.write_bytes(current.read_bytes())
            previous = self.digest(current)
            entries[destination] = {
                "source": source,
                "existed": True,
                "desired_sha256": "d" * 64,
                "desired_size": 9,
                "previous_sha256": previous,
                "previous_size": current.stat().st_size,
                "previous_mode": stat.S_IMODE(current.stat().st_mode),
                "previous_windows_state": {},
            }
        manifest = {
            "version": 2,
            "metadata_contract": recovery.WINDOWS_METADATA_CONTRACT,
            "install_root": str(self.root.resolve()),
            "created_at": "2026-10-28T00:00:00Z",
            "entries": entries,
        }
        self.write_json(self.backup / "rollback-manifest.json", manifest)

        original_manifest = {"version": 1, "entries": {}}
        self.write_json(self.legacy_backup / "rollback-manifest.json", original_manifest)
        legacy_receipt = {
            "result": "HOST_UPDATE_BLOCKED",
            "backup_dir": str(self.legacy_backup),
        }
        self.write_json(self.backup / "legacy-update-receipt.json", legacy_receipt)

        reconciliation = self.reconciliation(self.root, self.backup)
        receipt = {
            "result": "HOST_UPDATE_BLOCKED",
            "gateway_exit_code": recovery.updater.EXIT_HOST_UPDATE,
            "transaction_id": self.transaction_id,
            "source_commit": self.incident,
            "blocked_at": "2026-10-28T00:00:01Z",
            "phase": "install_runtime",
            "error": "blocked",
            "failure": {
                "type": "PermissionError",
                "cause_determined": False,
                "retry_policy": "requires_changed_precondition_and_new_transaction",
            },
            "backup_dir": str(self.backup),
            "mutation_started": True,
            "rollback_attempted": True,
            "rollback_succeeded": True,
            "rollback_error": None,
            "reconciliation": reconciliation,
            "legacy_v1_rebaseline": {
                "admitted": True,
                "historical_metadata_verified": False,
                "original_receipt_archived": True,
            },
            "receipt_path": str(self.root / "update-receipt.json"),
        }
        self.write_json(self.root / "update-receipt.json", receipt)
        journal = {
            "version": 1,
            "transaction_id": self.transaction_id,
            "source_commit": self.incident,
            "backup_dir": str(self.backup),
            "prepared_at": "2026-10-28T00:00:00Z",
        }
        self.write_json(self.root / "update-transaction.json", journal)

        pin_values = {
            "PRIMARY_RECEIPT_SHA256": self.digest(self.root / "update-receipt.json"),
            "ROLLBACK_V2_SHA256": self.digest(self.backup / "rollback-manifest.json"),
            "LEGACY_RECEIPT_SHA256": self.digest(self.backup / "legacy-update-receipt.json"),
            "LEGACY_MANIFEST_SHA256": self.digest(self.legacy_backup / "rollback-manifest.json"),
            "JOURNAL_SHA256": self.digest(self.root / "update-transaction.json"),
        }
        for name, value in pin_values.items():
            patcher = mock.patch.object(recovery, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def archive(self) -> Path:
        return self.backup / f"recovered-update-transaction-{self.transaction_id}.json"

    def test_success_moves_only_journal_and_replay_is_read_only(self) -> None:
        before = {
            path: self.digest(path)
            for path in (
                self.root / "update-receipt.json",
                self.backup / "rollback-manifest.json",
                self.backup / "legacy-update-receipt.json",
                self.legacy_backup / "rollback-manifest.json",
                *(self.root / item for item in self.managed.values()),
            )
        }
        result = recovery.recover(self.root)
        self.assertEqual(result["result"], "NOTERI_GATEWAY_JOURNAL_RECOVERED")
        self.assertFalse((self.root / "update-transaction.json").exists())
        self.assertTrue(self.archive().is_file())
        for path, digest in before.items():
            self.assertEqual(self.digest(path), digest)
        replay = recovery.recover(self.root)
        self.assertEqual(
            replay["result"],
            "NOTERI_GATEWAY_JOURNAL_ALREADY_RECOVERED_VERIFIED",
        )
        for path, digest in before.items():
            self.assertEqual(self.digest(path), digest)

    def test_digest_mismatch_blocks_without_move(self) -> None:
        with mock.patch.object(recovery, "JOURNAL_SHA256", "0" * 64):
            with self.assertRaisesRegex(recovery.RecoveryBlocked, "JOURNAL_DIGEST_MISMATCH"):
                recovery.recover(self.root)
        self.assertTrue((self.root / "update-transaction.json").is_file())
        self.assertFalse(self.archive().exists())

    def test_both_source_and_archive_present_block(self) -> None:
        self.archive().write_bytes((self.root / "update-transaction.json").read_bytes())
        with self.assertRaisesRegex(
            recovery.RecoveryBlocked, "JOURNAL_AND_ARCHIVE_BOTH_PRESENT"
        ):
            recovery.recover(self.root)

    def test_missing_lock_is_not_created(self) -> None:
        (self.root / "update.lock").unlink()
        with self.assertRaisesRegex(recovery.RecoveryBlocked, "UPDATE_LOCK_INVALID"):
            recovery.recover(self.root)
        self.assertFalse((self.root / "update.lock").exists())

    def test_duplicate_json_key_blocks(self) -> None:
        target = self.root / "update-transaction.json"
        target.write_text(
            '{"version":1,"version":1,"transaction_id":"' + self.transaction_id
            + '","source_commit":"' + self.incident + '","backup_dir":"'
            + str(self.backup).replace("\\", "\\\\")
            + '","prepared_at":"x"}\n',
            encoding="utf-8",
        )
        with mock.patch.object(recovery, "JOURNAL_SHA256", self.digest(target)):
            with self.assertRaisesRegex(recovery.RecoveryBlocked, "JSON_DUPLICATE_KEY"):
                recovery.recover(self.root)

    def test_changed_second_read_blocks_before_move(self) -> None:
        real = recovery._inspect
        calls = 0

        def changing(root: Path, journal: Path):
            nonlocal calls
            calls += 1
            value = real(root, journal)
            if calls == 2:
                return recovery.Evidence(
                    **{**value.__dict__, "reconciliation_json": "changed"}
                )
            return value

        with mock.patch.object(recovery, "_inspect", side_effect=changing):
            with self.assertRaisesRegex(
                recovery.RecoveryBlocked, "EVIDENCE_CHANGED_DURING_PREFLIGHT"
            ):
                recovery.recover(self.root)
        self.assertTrue((self.root / "update-transaction.json").is_file())

    def test_post_move_readback_failure_rolls_back_original_name(self) -> None:
        real = recovery._inspect
        calls = 0

        def fail_after_move(root: Path, journal: Path):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise recovery.RecoveryBlocked("injected-post-move")
            return real(root, journal)

        with mock.patch.object(recovery, "_inspect", side_effect=fail_after_move):
            with self.assertRaisesRegex(
                recovery.RecoveryBlocked,
                "POST_RECOVERY_READBACK_FAILED_ROLLED_BACK",
            ):
                recovery.recover(self.root)
        self.assertTrue((self.root / "update-transaction.json").is_file())
        self.assertEqual(
            self.digest(self.root / "update-transaction.json"),
            recovery.JOURNAL_SHA256,
        )
        self.assertFalse(self.archive().exists())

    def test_broken_archive_symlink_counts_as_existing(self) -> None:
        archive = self.archive()
        if os.name == "nt":
            real_lexists = os.path.lexists

            def lexists(path: object) -> bool:
                return Path(path) == archive or real_lexists(path)

            with mock.patch.object(recovery.os.path, "lexists", side_effect=lexists):
                with self.assertRaisesRegex(
                    recovery.RecoveryBlocked, "RECOVERY_ARCHIVE_ALREADY_EXISTS"
                ):
                    recovery.recover(self.root)
            return

        archive.symlink_to(self.root / "missing-target")
        with self.assertRaisesRegex(
            recovery.RecoveryBlocked, "JOURNAL_AND_ARCHIVE_BOTH_PRESENT"
        ):
            recovery.recover(self.root)


if __name__ == "__main__":
    unittest.main()

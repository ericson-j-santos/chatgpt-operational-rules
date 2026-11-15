"""Pure local regression and negative checks for read-only incident pin inventory."""
from __future__ import annotations
import importlib.util
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / 'scripts' / 'noteri_journal_50227f_readonly_pins.py'
spec = importlib.util.spec_from_file_location('noteri_readonly_pins', SOURCE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class ReadonlyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'gateway'
        self.root.mkdir()
        self.backup = self.root / mod.BACKUP_NAME
        entries = {}
        for rel in mod.MANAGED:
            payload = f'previous:{rel}\n'.encode()
            save(self.root / rel, payload)
            save(self.backup / 'files' / rel, payload)
            entries[rel] = {'existed': True, 'previous_sha256': mod.digest(payload),
                            'previous_size': len(payload), 'previous_windows_state': {}}
        self.write_json('update-transaction.json', {'version': 1, 'transaction_id': mod.TX,
                        'source_commit': mod.RULES, 'backup_dir': str(self.backup),
                        'prepared_at': '2026-11-12T22:58:30Z'})
        self.write_json(f'update-recovery-{mod.TX}.json', {
                        'result': 'HOST_UPDATE_BLOCKED', 'transaction_id': mod.TX,
                        'source_commit': mod.RULES, 'backup_dir': str(self.backup),
                        'rollback_succeeded': True, 'reconciliation': {'previous_state': True}})
        self.write_json('update-receipt.json', {
                        'source_commit': mod.PREVIOUS, 'result': 'HOST_UPDATE_OK',
                        'rules_version': '1.6.20'})
        self.write_json(f'{mod.BACKUP_NAME}/rollback-manifest.json', {
                        'version': 2, 'metadata_contract': 'windows-owner-group-dacl-ads-v1',
                        'install_root': str(self.root), 'entries': entries})

    def write_json(self, name, obj):
        save(self.root / name, (json.dumps(obj, sort_keys=True) + '\n').encode())

    def test_positive_no_mutation_and_four_pins(self):
        before = {p: mod.digest(p.read_bytes()) for p in self.root.rglob('*') if p.is_file()}
        result = mod.inspect(self.root, require_metadata=False)
        after = {p: mod.digest(p.read_bytes()) for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(result['result'], 'NOTERI_JOURNAL_50227F_READONLY_ATTESTED')
        self.assertEqual(set(result['hashes']), {'journal', 'failure', 'primary', 'manifest'})
        self.assertEqual(result['previous_files_content_verified'], 7)
        self.assertFalse(result['windows_security_metadata_verified'])
        self.assertFalse(result['recovery_authorized'])

    def test_metadata_gate_requires_native_verification(self):
        with mock.patch.object(mod, 'attest_windows_security', return_value=True) as attest:
            report = mod.inspect(self.root, require_metadata=True)
        self.assertTrue(report['windows_security_metadata_verified'])
        attest.assert_called_once()

    def test_metadata_gate_fails_closed(self):
        with mock.patch.object(mod, 'attest_windows_security', side_effect=mod.Blocked('WINDOWS_METADATA_CHANGED')):
            with self.assertRaisesRegex(mod.Blocked, 'WINDOWS_METADATA_CHANGED'):
                mod.inspect(self.root, require_metadata=True)

    def test_negative_wrong_incident(self):
        x = {'version': 1, 'transaction_id': 'f' * 32,
             'source_commit': mod.RULES, 'backup_dir': str(self.backup)}
        self.write_json('update-transaction.json', x)
        with self.assertRaisesRegex(mod.Blocked, 'JOURNAL_BINDING_INVALID'):
            mod.inspect(self.root, require_metadata=False)

    def test_negative_missing_rollback(self):
        (self.backup / 'files' / mod.MANAGED[0]).unlink()
        with self.assertRaises(mod.Blocked):
            mod.inspect(self.root, require_metadata=False)

    def test_negative_tampered_byte(self):
        target = self.root / mod.MANAGED[-1]
        save(target, b'altered\n')
        with self.assertRaisesRegex(mod.Blocked, 'ROLLBACK_FILE_DIVERGENT'):
            mod.inspect(self.root, require_metadata=False)

    def test_negative_symlink_in_path(self):
        original = self.root / mod.MANAGED[0]
        other = original.with_name('redirected.py')
        original.rename(other)
        try:
            original.symlink_to(other)
        except (NotImplementedError, OSError):
            self.skipTest('symlink unavailable on this machine')
        with self.assertRaises(mod.Blocked):
            mod.inspect(self.root, require_metadata=False)

    def test_negative_duplicate_json_key(self):
        save(self.root / 'update-transaction.json', b'{"version": 1, "version": 2}')
        with self.assertRaisesRegex(mod.Blocked, 'INVALID_JSON'):
            mod.inspect(self.root, require_metadata=False)

    def test_negative_wrong_manifest_shape(self):
        target = self.backup / 'rollback-manifest.json'
        obj = json.loads(target.read_text())
        obj['entries'].pop(mod.MANAGED[0])
        save(target, json.dumps(obj).encode())
        with self.assertRaisesRegex(mod.Blocked, 'MANIFEST_INVALID'):
            mod.inspect(self.root, require_metadata=False)

    def test_negative_stale_receipt(self):
        self.write_json('update-receipt.json', {'result': 'HOST_UPDATE_OK',
                                               'rules_version': '1.6.21',
                                               'source_commit': mod.PREVIOUS})
        with self.assertRaisesRegex(mod.Blocked, 'PRIMARY_RECEIPT_INVALID'):
            mod.inspect(self.root, require_metadata=False)

    def test_negative_read_race(self):
        original = mod.read_stable
        fired = {'done': False}
        def read(path, root, maximum=4*1024*1024):
            v = original(path, root, maximum)
            if not fired['done'] and path.name == 'update-transaction.json':
                fired['done'] = True
                path.write_bytes(path.read_bytes() + b' ')
            return v
        with mock.patch.object(mod, 'read_stable', side_effect=read):
            with self.assertRaisesRegex(mod.Blocked, 'EVIDENCE_CHANGED'):
                mod.inspect(self.root, require_metadata=False)


if __name__ == '__main__':
    unittest.main()

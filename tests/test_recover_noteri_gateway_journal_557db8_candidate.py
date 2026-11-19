"""Ensaios locais sem tocar no Windows, no journal real ou no GitHub."""
import importlib.util
import json
import hashlib
import io
import os
import sys
import tempfile
import types
import unittest
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1] / 'scripts' / 'recover_noteri_gateway_journal_557db8_candidate.py'

class RecoveryBlocked(RuntimeError):
    pass

baseline = types.ModuleType('recover_noteri_gateway_journal')
baseline.RecoveryBlocked = RecoveryBlocked
baseline._canonical_install_root = lambda p: p
baseline._lexists = lambda p: Path(p).exists()
baseline._is_reparse = lambda m: False
baseline.ExistingUpdateLock = lambda p: nullcontext()
baseline._move_no_replace = lambda a, b: Path(a).rename(b)
baseline._confined = lambda r, t, error: t
baseline._validate_manifest = lambda m, r: None
baseline._verified_reconciliation = lambda r, b: {"previous_state": True}
baseline._require_chain = lambda r, t, leaf: None
baseline._strict_json = lambda p: (json.loads(Path(p).read_bytes()), 'bad')
bootstrap = types.ModuleType('install_command_gateway_host')
updater = types.ModuleType('update_command_gateway_host')
updater.runtime_map_for_profile = lambda enabled, platform, host: {
    "config/command-gateway.policy.noteri-wsl.json": "config/policy.json"
} if (enabled, platform, host) == (True, "nt", "Noteri") else {}
# A fixture injeta dependências simuladas somente durante a importação do
# módulo candidato. Restaurar sys.modules antes que pytest colete outras suítes.
_saved_modules = {
    name: sys.modules.get(name)
    for name in (baseline.__name__, bootstrap.__name__, updater.__name__,
                 'recovery_candidate_under_test')
}
sys.modules.update({baseline.__name__: baseline, bootstrap.__name__: bootstrap, updater.__name__: updater})
spec = importlib.util.spec_from_file_location('recovery_candidate_under_test', BASE)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
try:
    spec.loader.exec_module(mod)
finally:
    for name, previous in _saved_modules.items():
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous

class RecoveryCandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.journal = self.root / 'update-transaction.json'
        self.journal.write_text('{}', encoding='utf-8')
        self.backup = self.root / ('backup-20261119-175724-' + 'a'*32)
        self.backup.mkdir()
        self.archive = self.backup / ('recovered-update-transaction-' + mod.TX_ID + '.json')
        self.evidence = mod.Evidence(mod.TX_ID, str(self.archive), '1'*64, '2'*64, '3'*64, '4'*64, (), '{}')
        self.fake_os = types.SimpleNamespace(name='nt', path=os.path, lstat=os.lstat)
        self.patcher = mock.patch.multiple(mod, os=self.fake_os, socket=types.SimpleNamespace(gethostname=lambda: "Noteri"), EXPECTED_INSTALL=self.root)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_cli_uses_only_pinned_install_root(self):
        digest = hashlib.sha256(BASE.read_bytes()).hexdigest()
        with mock.patch.object(mod, 'check', return_value={'result': 'FORENSIC_READONLY'}) as checked, \
             mock.patch.object(bootstrap, 'default_install_root', create=True,
                               side_effect=AssertionError('never use fallback root')), \
             mock.patch.object(sys, 'argv', ['recover', '--mode', 'inspect',
                                              '--expected-self-sha256', digest]), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(mod.main(), 0)
        checked.assert_called_once_with(mod.EXPECTED_INSTALL, recover=False)

    def test_readonly_success_has_no_move(self):
        with mock.patch.object(mod, '_inspect', return_value=self.evidence), mock.patch.object(baseline, '_move_no_replace') as move:
            result = mod.check(self.root, recover=False)
        self.assertEqual(result['result'], 'FORENSIC_READONLY')
        self.assertTrue(self.journal.exists())
        move.assert_not_called()

    def test_provisional_pins_block_mutation(self):
        with mock.patch.object(mod, '_inspect', return_value=self.evidence), mock.patch.object(baseline, '_move_no_replace') as move:
            with self.assertRaisesRegex(RecoveryBlocked, 'PINS_NOT_ADMITTED'):
                mod.check(self.root, recover=True)
        move.assert_not_called()
        self.assertTrue(self.journal.exists())

    def test_no_journal_or_archive_fail_closed(self):
        self.journal.unlink()
        with self.assertRaisesRegex(RecoveryBlocked, 'AMBIGUOUS'):
            mod.check(self.root, recover=False)

    def test_journal_and_archive_fail_closed(self):
        self.archive.write_bytes(b'{}')
        with self.assertRaisesRegex(RecoveryBlocked, 'AMBIGUOUS'):
            mod.check(self.root, recover=False)

    def test_unrelated_archived_transaction_is_not_this_incident(self):
        # Existing old backup must not block the current transaction.
        unrelated_backup = self.root / ('backup-20261031-120000-' + 'a' * 32)
        unrelated_backup.mkdir()
        (unrelated_backup / ('recovered-update-transaction-' + 'f' * 32 + '.json')).write_text('{}')
        with mock.patch.object(mod, '_inspect', return_value=self.evidence):
            result = mod.check(self.root, recover=False)
        self.assertEqual(result['result'], 'FORENSIC_READONLY')

    def test_same_transaction_archive_elsewhere_is_ambiguous(self):
        unrelated_backup = self.root / ('backup-20261031-120000-' + 'a' * 32)
        unrelated_backup.mkdir()
        (unrelated_backup / ('recovered-update-transaction-' + mod.TX_ID + '.json')).write_text('{}')
        with self.assertRaisesRegex(RecoveryBlocked, 'AMBIGUOUS'):
            mod.check(self.root, recover=False)

    def test_double_read_change_fail_closed(self):
        other = mod.Evidence(mod.TX_ID, str(self.archive), '9'*64, '2'*64, '3'*64, '4'*64, (), '{}')
        with mock.patch.object(mod, '_inspect', side_effect=(self.evidence, other)):
            with self.assertRaisesRegex(RecoveryBlocked, 'DOUBLE_READ_DIVERGED'):
                mod.check(self.root, recover=False)
        self.assertTrue(self.journal.exists())

    def test_host_mismatch_fail_closed(self):
        fake = types.SimpleNamespace(name='posix', path=os.path, lstat=os.lstat)
        with mock.patch.object(mod, 'os', fake):
            with self.assertRaisesRegex(RecoveryBlocked, 'NOTERI_WINDOWS_REQUIRED'):
                mod.check(self.root, recover=False)

    def test_candidate_digest_not_equal_fails(self):
        with self.assertRaisesRegex(RecoveryBlocked, 'JOURNAL_PIN_MISMATCH'):
            mod._read_pinned(self.root, self.journal, 'journal')

    def test_no_mutation_by_default(self):
        self.assertEqual(mod.PIN_STATUS, 'HOST_ATTESTED_MUTATION_NOT_AUTHORIZED')
        self.assertEqual(mod.CONFIRMATION, 'RECOVER_NOTERI_GATEWAY_TX_557DB8_ONCE')

    def test_readback_move_and_idempotent_replay_in_test_only(self):
        # Admissão SIMULADA, não válida no Noteri: valida somente o controle de fluxo.
        def _move(src, dest):
            self.assertFalse(dest.exists())
            src.rename(dest)
        with mock.patch.object(mod, 'PIN_STATUS', 'GATEWAY_ATTESTED_AND_APPROVED'), \
             mock.patch.object(mod, '_inspect', return_value=self.evidence), \
             mock.patch.object(baseline, '_move_no_replace', side_effect=_move):
            r = mod.check(self.root, recover=True)
            self.assertEqual(r['result'], 'JOURNAL_RECOVERED')
            self.assertFalse(self.journal.exists())
            self.assertTrue(self.archive.exists())
            r2 = mod.check(self.root, recover=True)
            self.assertEqual(r2['result'], 'ALREADY_RECOVERED_VERIFIED')
            self.assertFalse(self.journal.exists())


    def _incident_fixtures(self):
        manifest = {'entries': {'bin/session_launcher.py': {
            'existed': True, 'previous_windows_state': {'kind': 'expected'}
        }}}
        receipt = {'result': 'HOST_UPDATE_OK', 'rules_version': '1.6.20',
                   'source_commit': mod.PREVIOUS_RULES_SHA}
        failure = {'transaction_id': mod.TX_ID, 'result': 'HOST_UPDATE_BLOCKED',
                   'source_commit': mod.FAILED_RULES_SHA, 'phase': 'install_runtime',
                   'mutation_started': True, 'rollback_attempted': True,
                   'rollback_succeeded': True, 'rollback_error': None,
                   'reconciliation': {'previous_state': True},
                   'backup_dir': str(self.backup),
                   'failure': {'category': 'install_runtime_os_error',
                               'destination': 'bin/session_launcher.py',
                               'operation': 'activate_stage',
                               'os_error': {'winerror': 5},
                               'cause_determined': False}}
        journal = {'version': 1, 'transaction_id': mod.TX_ID,
                   'source_commit': mod.FAILED_RULES_SHA,
                   'backup_dir': str(self.backup), 'prepared_at': '2026-11-19T20:57:24Z'}
        return {'primary': receipt, 'failure': failure, 'manifest': manifest,
                'journal': journal}

    def _inspect_with(self, fixtures, *, windows_ok=True):
        def fake_pin(_root, _path, role):
            return fixtures[role], mod.SHA256_PINS_ATTESTED[role]
        with mock.patch.object(mod, '_read_pinned', side_effect=fake_pin), \
             mock.patch.object(baseline, '_validate_manifest') as schema_check, \
             mock.patch.object(baseline, '_verified_reconciliation',
                               return_value={'previous_state': True}), \
             mock.patch.object(bootstrap, '_validate_windows_persisted_state',
                               create=True), \
             mock.patch.object(bootstrap, '_windows_persisted_file_state',
                               return_value={'kind': 'expected'}, create=True), \
             mock.patch.object(bootstrap, '_windows_persisted_state_equivalent',
                               return_value=windows_ok, create=True):
            ev = mod._inspect(self.root, self.journal)
            self.assertEqual(schema_check.call_args.kwargs["runtime_map"], {
                "config/command-gateway.policy.noteri-wsl.json": "config/policy.json"
            })
            return ev

    def test_receipt_contract_positive_with_metadata(self):
        fixtures = self._incident_fixtures()
        ev = self._inspect_with(fixtures)
        self.assertEqual(ev.tx_id, mod.TX_ID)
        self.assertEqual(len(ev.windows_states), 1)

    def test_receipt_incorrect_winerror_refused(self):
        fixtures = self._incident_fixtures()
        fixtures['failure']['failure']['os_error']['winerror'] = 6
        with self.assertRaisesRegex(RecoveryBlocked, 'FAILURE_RECEIPT_INCIDENT_MISMATCH'):
            self._inspect_with(fixtures)

    def test_windows_metadata_mismatch_refused(self):
        fixtures = self._incident_fixtures()
        with self.assertRaisesRegex(RecoveryBlocked, 'WINDOWS_ROLLBACK_METADATA_MISMATCH'):
            self._inspect_with(fixtures, windows_ok=False)

    def test_live_reconciliation_false_blocks_journal(self):
        fixtures = self._incident_fixtures()
        def fake_pin(_root, _path, role):
            return fixtures[role], mod.SHA256_PINS_ATTESTED[role]
        with mock.patch.object(mod, '_read_pinned', side_effect=fake_pin), \
             mock.patch.object(baseline, '_validate_manifest'), \
             mock.patch.object(baseline, '_verified_reconciliation',
                               return_value={'previous_state': False}), \
             mock.patch.object(bootstrap, '_validate_windows_persisted_state',
                               create=True), \
             mock.patch.object(bootstrap, '_windows_persisted_file_state',
                               return_value={'kind': 'expected'}, create=True), \
             mock.patch.object(bootstrap, '_windows_persisted_state_equivalent',
                               return_value=True, create=True):
            with self.assertRaisesRegex(RecoveryBlocked, 'LIVE_ROLLBACK_RECONCILIATION_INVALID'):
                mod._inspect(self.root, self.journal)

    def test_recovery_is_blocked_before_any_mutation(self):
        with mock.patch.object(mod, '_inspect', return_value=self.evidence), \
             mock.patch.object(baseline, '_move_no_replace') as move:
            with self.assertRaisesRegex(RecoveryBlocked, 'PINS_NOT_ADMITTED_FOR_MUTATION'):
                mod.check(self.root, recover=True)
        move.assert_not_called()
        self.assertTrue(self.journal.exists())

if __name__ == '__main__':
    unittest.main()

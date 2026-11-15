#!/usr/bin/env python3
"""Recuperação governada da transação Noteri 50227f (candidata; não admitida).

Examina journal, recibos e rollback do incidente 12/11/2026. A execução
mutável permanece deliberadamente desabilitada até atestação dos hashes pelo
Gateway, Validator independente, integração, autorização Risk2 e readback.
Não edita ACL, conteúdo do runtime, recibo principal ou serviço.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCRIPTS = str(Path(__file__).resolve().parent)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

# Importar somente as primitivas oficiais já existentes na mesma árvore Git.
import recover_noteri_gateway_journal as base
import install_command_gateway_host as bootstrap

TX_ID = '50227f5817504ca4b328a962a076d673'
FAILED_RULES_SHA = '58dcd4200f412a866278027ba9e433439a03f3b3'
PREVIOUS_RULES_SHA = '11e2e86d0283e83247d8058d05bbabe3dd7cc2c2'
SHA256_PINS_ATTESTED = {
    'journal': '757ec16e15cf260b24438ab5e7f674eb0b15b6f0a7318dc27aeed6cfbf76c8d0',
    'failure': 'c93ec7e32489bf8c97e47d00b2f504d288aab38f0360a46107e1705db9ec21de',
    'primary': 'def5a5fc7f7748a61adac626420b3305aeb174f8db6332e48f94ca362393c741',
    'manifest': 'be7e55cbf943ce624df2528f99358140d304abe5a5b1888e19183166afc53be1',
}
PIN_STATUS = 'HOST_ATTESTED_MUTATION_NOT_AUTHORIZED'  # Risk2 depends on separate explicit approval/gates.
CONFIRMATION = 'RECOVER_NOTERI_GATEWAY_TX_50227F_ONCE'
EXPECTED_INSTALL = Path(r'C:\dev\chatgpt-command-gateway')


@dataclass(frozen=True)
class Evidence:
    tx_id: str
    archive: str
    journal_sha256: str
    failure_sha256: str
    primary_sha256: str
    manifest_sha256: str
    windows_states: tuple  # retido somente em memória, jamais publicado
    reconciliation: str


def _read_pinned(root: Path, path: Path, role: str) -> tuple[dict[str, Any], str]:
    base._require_chain(root, path, leaf='file')
    obj, digest = base._strict_json(path)
    if digest != SHA256_PINS_ATTESTED[role]:
        raise base.RecoveryBlocked(role.upper() + '_PIN_MISMATCH')
    return obj, digest


def _inspect(root: Path, journal_or_archive: Path) -> Evidence:
    primary, primary_sha = _read_pinned(root, root / 'update-receipt.json', 'primary')
    if not (
        primary.get('result') == 'HOST_UPDATE_OK'
        and primary.get('rules_version') == '1.6.20'
        and primary.get('source_commit') == PREVIOUS_RULES_SHA
    ):
        raise base.RecoveryBlocked('PREVIOUS_RUNTIME_RECEIPT_INVALID')

    failure, failure_sha = _read_pinned(
        root, root / ('update-recovery-' + TX_ID + '.json'), 'failure'
    )
    cause = failure.get('failure', {})
    os_error = cause.get('os_error', {}) if isinstance(cause, dict) else {}
    if not (
        failure.get('transaction_id') == TX_ID
        and failure.get('result') == 'HOST_UPDATE_BLOCKED'
        and failure.get('source_commit') == FAILED_RULES_SHA
        and failure.get('phase') == 'install_runtime'
        and failure.get('mutation_started') is True
        and failure.get('rollback_attempted') is True
        and failure.get('rollback_succeeded') is True
        and failure.get('rollback_error') is None
        and failure.get('reconciliation', {}).get('previous_state') is True
        and isinstance(cause, dict)
        and cause.get('category') == 'install_runtime_os_error'
        and cause.get('destination') == 'bin/session_preflight.py'
        and cause.get('operation') == 'activate_stage'
        and isinstance(os_error, dict)
        and os_error.get('winerror') == 5
        and cause.get('cause_determined') is False
    ):
        raise base.RecoveryBlocked('FAILURE_RECEIPT_INCIDENT_MISMATCH')

    backup_text = failure.get('backup_dir')
    if not isinstance(backup_text, str):
        raise base.RecoveryBlocked('BACKUP_REF_INVALID')
    backup = base._confined(root, Path(backup_text), 'BACKUP_OUTSIDE_INSTALL_ROOT')
    if not re.fullmatch(r'backup-[0-9]{8}-[0-9]{6}-[0-9a-f]{32}', backup.name):
        raise base.RecoveryBlocked('BACKUP_NAME_INVALID')
    base._require_chain(root, backup, leaf='directory')
    manifest, manifest_sha = _read_pinned(root, backup / 'rollback-manifest.json', 'manifest')
    base._validate_manifest(manifest, root)

    journal, journal_sha = _read_pinned(root, journal_or_archive, 'journal')
    if set(journal) != {'version', 'transaction_id', 'source_commit', 'backup_dir', 'prepared_at'}:
        raise base.RecoveryBlocked('JOURNAL_SCHEMA_INVALID')
    if not (
        journal['version'] == 1
        and journal['transaction_id'] == TX_ID
        and journal['source_commit'] == FAILED_RULES_SHA
        and journal['backup_dir'] == backup_text
        and isinstance(journal['prepared_at'], str)
        and failure.get('backup_dir') == journal['backup_dir']
    ):
        raise base.RecoveryBlocked('JOURNAL_INCIDENT_MISMATCH')

    # Comparar a configuração Windows (inclui identidade, ACL e ADS) dos sete
    # arquivos existentes e cópias do backup, além dos bytes anteriores.
    windows_states = []
    for relative, item in sorted(manifest['entries'].items()):
        current = root.joinpath(*relative.split('/'))
        saved = backup / 'files' / relative
        if item['existed'] is not True:
            if base._lexists(current) or base._lexists(saved):
                raise base.RecoveryBlocked('UNEXPECTED_MANAGED_FILE')
            continue
        base._require_chain(root, current, leaf='file')
        base._require_chain(root, saved, leaf='file')
        expected = item['previous_windows_state']
        bootstrap._validate_windows_persisted_state(expected, relative)
        current_state = bootstrap._windows_persisted_file_state(current)
        saved_state = bootstrap._windows_persisted_file_state(saved)
        if not (
            bootstrap._windows_persisted_state_equivalent(expected, current_state)
            and bootstrap._windows_persisted_state_equivalent(expected, saved_state)
        ):
            raise base.RecoveryBlocked('WINDOWS_ROLLBACK_METADATA_MISMATCH')
        windows_states.append((relative, current_state, saved_state))

    reconcile = base._verified_reconciliation(root, backup)
    archive = backup / ('recovered-update-transaction-' + TX_ID + '.json')
    base._confined(root, archive, 'RECOVERY_ARCHIVE_OUTSIDE_ROOT')
    return Evidence(
        TX_ID, str(archive), journal_sha, failure_sha, primary_sha,
        manifest_sha, tuple(windows_states),
        json.dumps(reconcile, sort_keys=True, separators=(',', ':')),
    )


def check(root: Path, *, recover: bool) -> dict[str, Any]:
    if os.name != 'nt' or socket.gethostname().casefold() != 'noteri':
        raise base.RecoveryBlocked('NOTERI_WINDOWS_REQUIRED')
    root = base._canonical_install_root(root)
    if os.path.normcase(str(root)) != os.path.normcase(str(EXPECTED_INSTALL)):
        raise base.RecoveryBlocked('INSTALL_ROOT_MISMATCH')
    journal = root / 'update-transaction.json'
    with base.ExistingUpdateLock(root / 'update.lock'):
        exists = base._lexists(journal)
        # Evita conflitos com qualquer recuperação anterior, inclusive de outra transação.
        archives = [p for b in root.iterdir()
                    if b.name.startswith('backup-') and b.is_dir()
                    and not base._is_reparse(os.lstat(b))
                    for p in b.iterdir()
                    if p.name == 'recovered-update-transaction-' + TX_ID + '.json']
        if len(archives) > 1 or (exists and archives) or (not exists and not archives):
            raise base.RecoveryBlocked('JOURNAL_ARCHIVE_STATE_AMBIGUOUS')
        observed = journal if exists else archives[0]
        first = _inspect(root, observed)
        second = _inspect(root, observed)
        if first != second:
            raise base.RecoveryBlocked('DOUBLE_READ_DIVERGED')
        archive = Path(first.archive)
        if not exists and os.path.normcase(str(observed)) != os.path.normcase(str(archive)):
            raise base.RecoveryBlocked('ARCHIVE_LOCATION_MISMATCH')

        if recover:
            if PIN_STATUS != 'GATEWAY_ATTESTED_AND_APPROVED':
                raise base.RecoveryBlocked('PINS_NOT_ADMITTED_FOR_MUTATION')
            if not exists:
                return _public_result('ALREADY_RECOVERED_VERIFIED', first)
            base._require_chain(root, archive, leaf='absent')
            base._move_no_replace(journal, archive)
            try:
                after_a, after_b = _inspect(root, archive), _inspect(root, archive)
                if base._lexists(journal) or after_a != first or after_b != first:
                    raise base.RecoveryBlocked('POST_MOVE_READBACK_DIVERGENT')
            except Exception as exc:
                if base._lexists(archive) and not base._lexists(journal):
                    base._move_no_replace(archive, journal)
                    if _inspect(root, journal) != first:
                        raise base.RecoveryBlocked('ROLLBACK_READBACK_FAILED') from exc
                raise base.RecoveryBlocked('MOVE_FAILED_ROLLED_BACK') from exc
            return _public_result('JOURNAL_RECOVERED', first)
        return _public_result('FORENSIC_READONLY', first)


def _public_result(result: str, ev: Evidence) -> dict[str, Any]:
    return dict(result=result, transaction_id=ev.tx_id,
                journal_sha256=ev.journal_sha256,
                failure_sha256=ev.failure_sha256,
                primary_sha256=ev.primary_sha256,
                rollback_manifest_sha256=ev.manifest_sha256,
                checked_windows_files=len(ev.windows_states),
                runtime_changed=False, primary_receipt_changed=False,
                recovery_allowed=(PIN_STATUS == 'GATEWAY_ATTESTED_AND_APPROVED'))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('inspect', 'recover'), default='inspect')
    parser.add_argument('--expected-self-sha256', required=True)
    parser.add_argument('--confirm', default='')
    args = parser.parse_args()
    try:
        actual = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        if not re.fullmatch(r'[0-9a-f]{64}', args.expected_self_sha256):
            raise base.RecoveryBlocked('SELF_SHA_REQUIRED')
        if actual != args.expected_self_sha256:
            raise base.RecoveryBlocked('SELF_SHA_MISMATCH')
        if args.mode == 'recover' and args.confirm != CONFIRMATION:
            raise base.RecoveryBlocked('RECOVERY_CONFIRMATION_REQUIRED')
        result = check(bootstrap.default_install_root(), recover=args.mode == 'recover')
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        reason = str(exc) if isinstance(exc, base.RecoveryBlocked) else 'RECOVERY_INTERNAL_BLOCKED'
        print(json.dumps(dict(result='NOTERI_JOURNAL_RECOVERY_BLOCKED', reason=reason), sort_keys=True))
        return 29


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""Recupera somente o journal do incidente fixo do Gateway no Noteri.

A operação não instala runtime, não altera recibos e não executa rollback. Ela
move o journal já qualificado para o backup v2 ao qual ele está vinculado,
sem overwrite, após duas leituras idênticas de conteúdo e metadados Windows.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import hashlib
import json
import os
import re
import socket
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import install_command_gateway_host as bootstrap
import update_command_gateway_host as updater


EXIT_RECOVERY_BLOCKED = 29
CONFIRMATION = "RECOVER_NOTERI_GATEWAY_JOURNAL_ONCE"
INCIDENT_RULES_SHA = "7d6ec0742ce9570408e8f3673a4f3c65c6777eb0"
JOURNAL_SHA256 = "9ac77a86c875f060b54727678accd24ce7ee895b0afc040ec51965831e2eea8d"
PRIMARY_RECEIPT_SHA256 = "4a0ee0fad909c63cb4cff89577720e5ee1cd4c9832cfe18e691e3555ba81c512"
ROLLBACK_V2_SHA256 = "2a689e8d3696368c08128da2b3ac4c1f15760005462de615f9b27d2e233f6e09"
LEGACY_RECEIPT_SHA256 = "4a4eaa3e9ca99afa053c371f9974da59c9f867a1f249b36dd79450e841e830bf"
LEGACY_MANIFEST_SHA256 = "f415f09b37ef30ac765f3616810c45bb8b99d018aa3a59b3a1b349aa7c558209"
WINDOWS_METADATA_CONTRACT = "windows-owner-group-dacl-ads-v1"
TRANSACTION_ID = re.compile(r"[0-9a-f]{32}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
MOVEFILE_WRITE_THROUGH = 0x8
GENERIC_READ = 0x80000000
OPEN_EXISTING = 3
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class RecoveryBlocked(RuntimeError):
    """Falha fechada com código estável e sem dados do host."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _is_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    )


def _require_regular(path: Path, code: str) -> os.stat_result:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise RecoveryBlocked(code) from exc
    if _is_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise RecoveryBlocked(code)
    return metadata


def _require_directory(path: Path, code: str) -> os.stat_result:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise RecoveryBlocked(code) from exc
    if _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise RecoveryBlocked(code)
    return metadata


def _confined(root: Path, target: Path, code: str) -> Path:
    root_absolute = Path(os.path.abspath(root))
    target_absolute = Path(os.path.abspath(target))
    try:
        common = os.path.commonpath([str(root_absolute), str(target_absolute)])
    except ValueError as exc:
        raise RecoveryBlocked(code) from exc
    if os.path.normcase(common) != os.path.normcase(str(root_absolute)):
        raise RecoveryBlocked(code)
    return target_absolute


def _require_chain(root: Path, target: Path, *, leaf: str) -> None:
    root = Path(os.path.abspath(root))
    target = _confined(root, target, "PATH_OUTSIDE_INSTALL_ROOT")
    _require_directory(root, "INSTALL_ROOT_INVALID")
    relative = target.relative_to(root)
    current = root
    for index, component in enumerate(relative.parts):
        current /= component
        if not _lexists(current):
            if index == len(relative.parts) - 1 and leaf == "absent":
                return
            raise RecoveryBlocked("EVIDENCE_PATH_MISSING")
        if index == len(relative.parts) - 1:
            if leaf == "file":
                _require_regular(current, "EVIDENCE_FILE_INVALID")
            elif leaf == "directory":
                _require_directory(current, "EVIDENCE_DIRECTORY_INVALID")
            elif leaf == "absent":
                raise RecoveryBlocked("RECOVERY_ARCHIVE_ALREADY_EXISTS")
        else:
            _require_directory(current, "EVIDENCE_PARENT_INVALID")


def _reject_constant(_value: str) -> None:
    raise RecoveryBlocked("JSON_NON_FINITE_VALUE")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RecoveryBlocked("JSON_DUPLICATE_KEY")
        result[key] = value
    return result


def _strict_json(path: Path, *, maximum: int = 4 * 1024 * 1024) -> tuple[dict[str, Any], str]:
    _require_regular(path, "JSON_EVIDENCE_INVALID")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RecoveryBlocked("JSON_EVIDENCE_UNREADABLE") from exc
    if not raw or len(raw) > maximum:
        raise RecoveryBlocked("JSON_EVIDENCE_SIZE_INVALID")
    digest = hashlib.sha256(raw).hexdigest()
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except RecoveryBlocked:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryBlocked("JSON_EVIDENCE_INVALID") from exc
    if not isinstance(value, dict):
        raise RecoveryBlocked("JSON_EVIDENCE_NOT_OBJECT")
    return value, digest


def _exact_keys(value: dict[str, Any], expected: set[str], code: str) -> None:
    if set(value) != expected:
        raise RecoveryBlocked(code)


def _validate_manifest(manifest: dict[str, Any], root: Path) -> None:
    _exact_keys(
        manifest,
        {"version", "metadata_contract", "install_root", "created_at", "entries"},
        "ROLLBACK_MANIFEST_SCHEMA_INVALID",
    )
    if (
        manifest["version"] != bootstrap.ROLLBACK_MANIFEST_VERSION
        or manifest["metadata_contract"] != WINDOWS_METADATA_CONTRACT
        or os.path.normcase(str(Path(manifest["install_root"])))
        != os.path.normcase(str(root.resolve(strict=True)))
        or not isinstance(manifest["created_at"], str)
        or not isinstance(manifest["entries"], dict)
        or set(manifest["entries"]) != set(updater.DEFAULT_RUNTIME_MAP.values())
    ):
        raise RecoveryBlocked("ROLLBACK_MANIFEST_INVALID")
    for destination, entry in manifest["entries"].items():
        if not isinstance(entry, dict):
            raise RecoveryBlocked("ROLLBACK_ENTRY_INVALID")
        existed = entry.get("existed")
        expected = {"source", "existed", "desired_sha256", "desired_size"}
        if existed is True:
            expected |= {
                "previous_sha256", "previous_size", "previous_mode",
                "previous_windows_state",
            }
        _exact_keys(entry, expected, "ROLLBACK_ENTRY_SCHEMA_INVALID")
        if (
            not isinstance(existed, bool)
            or entry["source"] not in updater.DEFAULT_RUNTIME_MAP
            or updater.DEFAULT_RUNTIME_MAP[entry["source"]] != destination
            or not SHA256.fullmatch(str(entry["desired_sha256"]))
            or isinstance(entry["desired_size"], bool)
            or not isinstance(entry["desired_size"], int)
        ):
            raise RecoveryBlocked("ROLLBACK_ENTRY_INVALID")
        if existed and (
            not SHA256.fullmatch(str(entry["previous_sha256"]))
            or isinstance(entry["previous_size"], bool)
            or not isinstance(entry["previous_size"], int)
            or isinstance(entry["previous_mode"], bool)
            or not isinstance(entry["previous_mode"], int)
            or not isinstance(entry["previous_windows_state"], dict)
        ):
            raise RecoveryBlocked("ROLLBACK_ENTRY_INVALID")


def _validate_incident_receipt(receipt: dict[str, Any], root: Path) -> None:
    expected = {
        "result", "gateway_exit_code", "transaction_id", "source_commit",
        "blocked_at", "phase", "error", "failure", "backup_dir",
        "mutation_started", "rollback_attempted", "rollback_succeeded",
        "rollback_error", "reconciliation", "legacy_v1_rebaseline",
        "receipt_path",
    }
    _exact_keys(receipt, expected, "PRIMARY_RECEIPT_SCHEMA_INVALID")
    failure = receipt.get("failure")
    if not isinstance(failure, dict):
        raise RecoveryBlocked("PRIMARY_FAILURE_SCHEMA_INVALID")
    _exact_keys(
        failure,
        {"type", "cause_determined", "retry_policy"},
        "PRIMARY_FAILURE_SCHEMA_INVALID",
    )
    reconciliation = receipt.get("reconciliation")
    if not isinstance(reconciliation, dict):
        raise RecoveryBlocked("PRIMARY_RECONCILIATION_INVALID")
    _exact_keys(
        reconciliation,
        {"overall", "previous_state", "desired_state", "entries"},
        "PRIMARY_RECONCILIATION_INVALID",
    )
    entries = reconciliation.get("entries")
    if not isinstance(entries, dict) or set(entries) != set(updater.DEFAULT_RUNTIME_MAP.values()):
        raise RecoveryBlocked("PRIMARY_RECONCILIATION_INVALID")
    for item in entries.values():
        if not isinstance(item, dict):
            raise RecoveryBlocked("PRIMARY_RECONCILIATION_INVALID")
        _exact_keys(item, {"state", "previous", "desired"}, "PRIMARY_RECONCILIATION_INVALID")
    legacy = receipt.get("legacy_v1_rebaseline")
    if not isinstance(legacy, dict):
        raise RecoveryBlocked("PRIMARY_LEGACY_BINDING_INVALID")
    _exact_keys(
        legacy,
        {"admitted", "historical_metadata_verified", "original_receipt_archived"},
        "PRIMARY_LEGACY_BINDING_INVALID",
    )
    if (
        receipt["result"] != "HOST_UPDATE_BLOCKED"
        or receipt["gateway_exit_code"] != updater.EXIT_HOST_UPDATE
        or receipt["source_commit"] != INCIDENT_RULES_SHA
        or receipt["phase"] != "install_runtime"
        or receipt["mutation_started"] is not True
        or receipt["rollback_attempted"] is not True
        or receipt["rollback_succeeded"] is not True
        or failure["type"] != "PermissionError"
        or reconciliation["previous_state"] is not True
        or legacy != {
            "admitted": True,
            "historical_metadata_verified": False,
            "original_receipt_archived": True,
        }
        or os.path.normcase(str(Path(receipt["receipt_path"])))
        != os.path.normcase(str(root / "update-receipt.json"))
    ):
        raise RecoveryBlocked("PRIMARY_RECEIPT_INCIDENT_MISMATCH")


def _validate_journal(journal: dict[str, Any], receipt: dict[str, Any]) -> None:
    _exact_keys(
        journal,
        {"version", "transaction_id", "source_commit", "backup_dir", "prepared_at"},
        "JOURNAL_SCHEMA_INVALID",
    )
    transaction_id = journal.get("transaction_id")
    if (
        journal.get("version") != 1
        or not isinstance(transaction_id, str)
        or not TRANSACTION_ID.fullmatch(transaction_id)
        or journal.get("source_commit") != INCIDENT_RULES_SHA
        or journal.get("transaction_id") != receipt.get("transaction_id")
        or journal.get("backup_dir") != receipt.get("backup_dir")
        or not isinstance(journal.get("prepared_at"), str)
    ):
        raise RecoveryBlocked("JOURNAL_BINDING_INVALID")


def _verified_reconciliation(root: Path, backup: Path) -> dict[str, Any]:
    try:
        observed = updater.reconcile_runtime(root, backup)
    except Exception as exc:
        raise RecoveryBlocked("RUNTIME_RECONCILIATION_BLOCKED") from exc
    if observed.get("previous_state") is not True:
        raise RecoveryBlocked("RUNTIME_NOT_RESTORED")
    entries = observed.get("entries")
    if not isinstance(entries, dict) or set(entries) != set(updater.DEFAULT_RUNTIME_MAP.values()):
        raise RecoveryBlocked("RUNTIME_RECONCILIATION_INVALID")
    if any(item.get("previous") is not True for item in entries.values()):
        raise RecoveryBlocked("RUNTIME_NOT_RESTORED")
    return observed


@dataclass(frozen=True)
class Evidence:
    transaction_id: str
    backup: str
    archive: str
    journal_sha256: str
    primary_receipt_sha256: str
    rollback_manifest_sha256: str
    legacy_receipt_sha256: str
    legacy_manifest_sha256: str
    reconciliation_json: str


def _inspect(root: Path, journal_path: Path) -> Evidence:
    root = root.resolve(strict=True)
    receipt_path = root / "update-receipt.json"
    _require_chain(root, receipt_path, leaf="file")
    receipt, receipt_digest = _strict_json(receipt_path)
    if receipt_digest != PRIMARY_RECEIPT_SHA256:
        raise RecoveryBlocked("PRIMARY_RECEIPT_DIGEST_MISMATCH")
    _validate_incident_receipt(receipt, root)

    backup = _confined(root, Path(str(receipt["backup_dir"])), "BACKUP_OUTSIDE_INSTALL_ROOT")
    _require_chain(root, backup, leaf="directory")
    manifest_path = backup / "rollback-manifest.json"
    _require_chain(root, manifest_path, leaf="file")
    manifest, manifest_digest = _strict_json(manifest_path)
    if manifest_digest != ROLLBACK_V2_SHA256:
        raise RecoveryBlocked("ROLLBACK_MANIFEST_DIGEST_MISMATCH")
    _validate_manifest(manifest, root)

    for destination, entry in manifest["entries"].items():
        managed = root.joinpath(*destination.split("/"))
        if entry["existed"]:
            _require_chain(root, managed, leaf="file")
            _require_chain(root, backup / "files" / destination, leaf="file")
        elif _lexists(managed) or _lexists(backup / "files" / destination):
            raise RecoveryBlocked("ABSENT_MANAGED_PATH_PRESENT")

    legacy_archive = backup / "legacy-update-receipt.json"
    _require_chain(root, legacy_archive, leaf="file")
    legacy_receipt, legacy_digest = _strict_json(legacy_archive)
    if legacy_digest != LEGACY_RECEIPT_SHA256:
        raise RecoveryBlocked("LEGACY_RECEIPT_DIGEST_MISMATCH")
    if legacy_receipt.get("result") != "HOST_UPDATE_BLOCKED":
        raise RecoveryBlocked("LEGACY_RECEIPT_INVALID")
    legacy_backup_text = legacy_receipt.get("backup_dir")
    if not isinstance(legacy_backup_text, str):
        raise RecoveryBlocked("LEGACY_MANIFEST_REFERENCE_INVALID")
    legacy_backup = _confined(root, Path(legacy_backup_text), "LEGACY_BACKUP_OUTSIDE_INSTALL_ROOT")
    _require_chain(root, legacy_backup, leaf="directory")
    legacy_manifest = legacy_backup / "rollback-manifest.json"
    _require_chain(root, legacy_manifest, leaf="file")
    _, legacy_manifest_digest = _strict_json(legacy_manifest)
    if legacy_manifest_digest != LEGACY_MANIFEST_SHA256:
        raise RecoveryBlocked("LEGACY_MANIFEST_DIGEST_MISMATCH")

    _require_chain(root, journal_path, leaf="file")
    journal, journal_digest = _strict_json(journal_path)
    if journal_digest != JOURNAL_SHA256:
        raise RecoveryBlocked("JOURNAL_DIGEST_MISMATCH")
    _validate_journal(journal, receipt)
    if os.path.normcase(str(Path(journal["backup_dir"]))) != os.path.normcase(str(backup)):
        raise RecoveryBlocked("JOURNAL_BACKUP_MISMATCH")
    archive = backup / f"recovered-update-transaction-{journal['transaction_id']}.json"
    _confined(root, archive, "RECOVERY_ARCHIVE_OUTSIDE_INSTALL_ROOT")
    reconciliation = _verified_reconciliation(root, backup)
    return Evidence(
        transaction_id=journal["transaction_id"],
        backup=str(backup),
        archive=str(archive),
        journal_sha256=journal_digest,
        primary_receipt_sha256=receipt_digest,
        rollback_manifest_sha256=manifest_digest,
        legacy_receipt_sha256=legacy_digest,
        legacy_manifest_sha256=legacy_manifest_digest,
        reconciliation_json=json.dumps(reconciliation, sort_keys=True, separators=(",", ":")),
    )


class ExistingUpdateLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.descriptor = -1
        self.handle: int | None = None

    def __enter__(self) -> "ExistingUpdateLock":
        before = _require_regular(self.path, "UPDATE_LOCK_INVALID")
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create = kernel32.CreateFileW
            create.argtypes = [
                ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
            ]
            create.restype = ctypes.c_void_p
            handle = create(
                str(self.path), GENERIC_READ, 0, None, OPEN_EXISTING,
                FILE_FLAG_OPEN_REPARSE_POINT, None,
            )
            if handle == INVALID_HANDLE_VALUE:
                raise RecoveryBlocked("UPDATE_LOCK_BUSY")
            self.handle = int(handle)
        else:
            import fcntl

            flags = os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0))
            self.descriptor = os.open(self.path, flags)
            try:
                fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                os.close(self.descriptor)
                self.descriptor = -1
                raise RecoveryBlocked("UPDATE_LOCK_BUSY") from exc
        after = _require_regular(self.path, "UPDATE_LOCK_CHANGED")
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            self.__exit__(None, None, None)
            raise RecoveryBlocked("UPDATE_LOCK_CHANGED")
        return self

    def __exit__(self, *_args: object) -> None:
        if self.handle is not None:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(
                ctypes.c_void_p(self.handle)
            )
            self.handle = None
        if self.descriptor >= 0:
            import fcntl

            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            os.close(self.descriptor)
            self.descriptor = -1


def _move_no_replace(source: Path, destination: Path) -> None:
    if _lexists(destination):
        raise RecoveryBlocked("RECOVERY_ARCHIVE_ALREADY_EXISTS")
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        move = kernel32.MoveFileExW
        move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        move.restype = ctypes.c_int
        if not move(str(source), str(destination), MOVEFILE_WRITE_THROUGH):
            raise RecoveryBlocked("JOURNAL_MOVE_BLOCKED")
    else:
        try:
            os.link(source, destination, follow_symlinks=False)
            os.unlink(source)
        except OSError as exc:
            with contextlib.suppress(OSError):
                if _lexists(destination) and _lexists(source):
                    destination.unlink()
            raise RecoveryBlocked("JOURNAL_MOVE_BLOCKED") from exc


def _result_payload(result: str, evidence: Evidence) -> dict[str, Any]:
    return {
        "result": result,
        "recovery_contract": "noteri-gateway-journal-recovery-v1",
        "incident_rules_sha": INCIDENT_RULES_SHA,
        "transaction_id": evidence.transaction_id,
        "journal_sha256": evidence.journal_sha256,
        "primary_receipt_sha256": evidence.primary_receipt_sha256,
        "rollback_manifest_sha256": evidence.rollback_manifest_sha256,
        "legacy_receipt_sha256": evidence.legacy_receipt_sha256,
        "legacy_manifest_sha256": evidence.legacy_manifest_sha256,
        "metadata_contract": WINDOWS_METADATA_CONTRACT,
        "managed_file_count": len(updater.DEFAULT_RUNTIME_MAP),
        "journal_present": False,
        "recovery_archive_present": True,
        "owner_group_dacl_ads_verified": True,
        "managed_runtime_changed": False,
        "primary_receipt_changed": False,
    }


def recover(root: Path) -> dict[str, Any]:
    root = Path(os.path.abspath(root))
    _require_directory(root, "INSTALL_ROOT_INVALID")
    lock_path = root / "update.lock"
    journal_path = root / "update-transaction.json"
    with ExistingUpdateLock(lock_path):
        source_exists = _lexists(journal_path)
        archive_candidates = []
        for child in root.iterdir():
            if child.name.startswith("backup-") and child.is_dir() and not _is_reparse(os.lstat(child)):
                archive_candidates.extend(
                    item for item in child.iterdir()
                    if item.name.startswith("recovered-update-transaction-")
                    and item.name.endswith(".json")
                )
        if len(archive_candidates) > 1:
            raise RecoveryBlocked("RECOVERY_ARCHIVE_AMBIGUOUS")
        existing_archive = archive_candidates[0] if archive_candidates else None
        if source_exists and existing_archive is not None:
            raise RecoveryBlocked("JOURNAL_AND_ARCHIVE_BOTH_PRESENT")
        if not source_exists and existing_archive is None:
            raise RecoveryBlocked("JOURNAL_AND_ARCHIVE_BOTH_ABSENT")

        observed_path = journal_path if source_exists else existing_archive
        assert observed_path is not None
        first = _inspect(root, observed_path)
        second = _inspect(root, observed_path)
        if first != second:
            raise RecoveryBlocked("EVIDENCE_CHANGED_DURING_PREFLIGHT")

        archive = Path(first.archive)
        if source_exists:
            _require_chain(root, archive, leaf="absent")
            _move_no_replace(journal_path, archive)
            if _lexists(journal_path) or not _lexists(archive):
                raise RecoveryBlocked("JOURNAL_MOVE_POSTCONDITION_FAILED")
            try:
                after_first = _inspect(root, archive)
                after_second = _inspect(root, archive)
                if after_first != first or after_second != first:
                    raise RecoveryBlocked("EVIDENCE_CHANGED_AFTER_RECOVERY")
            except Exception as exc:
                try:
                    if _lexists(journal_path) or not _lexists(archive):
                        raise RecoveryBlocked("RECOVERY_ROLLBACK_PRECONDITION_FAILED")
                    _move_no_replace(archive, journal_path)
                    if not _lexists(journal_path) or _lexists(archive):
                        raise RecoveryBlocked("RECOVERY_ROLLBACK_POSTCONDITION_FAILED")
                    rollback_first = _inspect(root, journal_path)
                    rollback_second = _inspect(root, journal_path)
                    if rollback_first != first or rollback_second != first:
                        raise RecoveryBlocked("RECOVERY_ROLLBACK_READBACK_FAILED")
                except Exception as rollback_exc:
                    raise RecoveryBlocked("RECOVERY_ROLLBACK_FAILED") from rollback_exc
                raise RecoveryBlocked("POST_RECOVERY_READBACK_FAILED_ROLLED_BACK") from exc
            return _result_payload("NOTERI_GATEWAY_JOURNAL_RECOVERED", first)

        if os.path.normcase(str(existing_archive)) != os.path.normcase(str(archive)):
            raise RecoveryBlocked("RECOVERY_ARCHIVE_NAME_INVALID")
        return _result_payload(
            "NOTERI_GATEWAY_JOURNAL_ALREADY_RECOVERED_VERIFIED", first
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Recuperação fixa do journal Gateway Noteri")
    parser.add_argument("--expected-self-sha256", required=True)
    parser.add_argument("--confirm", required=True)
    ns = parser.parse_args()
    phase = "admission"
    try:
        if os.name != "nt" or socket.gethostname().casefold() != "noteri":
            raise RecoveryBlocked("NOTERI_WINDOWS_REQUIRED")
        if ns.confirm != CONFIRMATION:
            raise RecoveryBlocked("RECOVERY_CONFIRMATION_REQUIRED")
        expected = ns.expected_self_sha256.strip().lower()
        actual = sha256_file(Path(__file__).resolve())
        if not SHA256.fullmatch(expected) or actual != expected:
            raise RecoveryBlocked("RECOVERY_SCRIPT_DIGEST_MISMATCH")
        phase = "recover_journal"
        payload = recover(bootstrap.default_install_root())
        print(json.dumps(payload, sort_keys=True))
        return 0
    except Exception as exc:
        reason = str(exc) if isinstance(exc, RecoveryBlocked) else "RECOVERY_INTERNAL_BLOCKED"
        print(
            json.dumps(
                {
                    "result": "NOTERI_GATEWAY_JOURNAL_RECOVERY_BLOCKED",
                    "phase": phase,
                    "reason": reason,
                    "managed_runtime_changed": False,
                    "primary_receipt_changed": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return EXIT_RECOVERY_BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())

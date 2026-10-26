"""Noteri-only legacy Gateway v1 recovery admission (never rewrites v1).

The historical v1 backup lacks Windows owner/group/DACL/ADS evidence. This
module does NOT 'upgrade' the historical manifest. It verifies the old bytes,
checks live Windows metadata, and permits a NEW independent v2 baseline only
inside an explicitly authorized updater transaction.

No WSL, service, credential, privilege, ACL, or managed-runtime write is made
by verify_legacy_v1_current_state(). The official updater owns v2 snapshot,
exclusive lock, replaceability preflight and rollback.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
from pathlib import Path
from typing import Any

import install_command_gateway_host as bootstrap

LEGACY_SOURCE_COMMIT = "8a34b2b0862affb98a9e094e89802fdc6df090c0"
HOST = "noteri"
INSTALL_ROOT = Path(r"C:\dev\chatgpt-command-gateway")
BACKUP_RE = re.compile(r"backup-[0-9]{8}-[0-9]{6}-[0-9a-f]{32}\Z")
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
MAX_JSON_BYTES = 256 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024


class LegacyBlocked(bootstrap.HostBootstrapError):
    """Fail-closed with fixed, non-sensitive categories."""


def _deny(reason: str) -> None:
    raise LegacyBlocked(reason)


def _host_is_noteri_windows() -> bool:
    return os.name == "nt" and socket.gethostname().casefold() == HOST


def _required_install_root() -> Path:
    return INSTALL_ROOT


def _confined_regular(path: Path, root: Path, *, max_bytes: int) -> bytes:
    root = root.resolve(strict=True)
    try:
        if os.path.commonpath((str(root), str(path.resolve(strict=True)))) != str(root):
            _deny("LEGACY_PATH_OUTSIDE_ROOT")
    except (OSError, ValueError):
        _deny("LEGACY_PATH_INVALID")
    for item in (path, *path.parents):
        if item == root.parent:
            break
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0) & 0x400
        ):
            _deny("LEGACY_REPARSE_DENIED")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > max_bytes:
        _deny("LEGACY_FILE_SHAPE_INVALID")
    payload = path.read_bytes()
    after = path.stat()
    if len(payload) != info.st_size or (after.st_size, after.st_mtime_ns) != (
        info.st_size, info.st_mtime_ns
    ):
        _deny("LEGACY_FILE_CHANGED")
    return payload


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _expected_hash(value: str, kind: str) -> None:
    if type(value) is not str or SHA_RE.fullmatch(value) is None:
        _deny(kind)


def _read_json(path: Path, root: Path) -> tuple[dict[str, Any], str]:
    payload = _confined_regular(path, root, max_bytes=MAX_JSON_BYTES)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        _deny("LEGACY_JSON_INVALID")
    if type(value) is not dict:
        _deny("LEGACY_JSON_INVALID")
    return value, _digest(payload)


def verify_legacy_v1_current_state(
    install_root: Path, *, expected_receipt_sha256: str,
    expected_manifest_sha256: str,
) -> dict[str, Any]:
    """Read-only, strict 7-file admission; v1 does not authenticate past ACL/ADS."""
    if not _host_is_noteri_windows() or not bootstrap._windows_metadata_required():
        _deny("LEGACY_NOTERI_WINDOWS_REQUIRED")
    if install_root.resolve() != _required_install_root().resolve():
        _deny("LEGACY_INSTALL_ROOT_DENIED")
    _expected_hash(expected_receipt_sha256, "LEGACY_RECEIPT_PIN_INVALID")
    _expected_hash(expected_manifest_sha256, "LEGACY_MANIFEST_PIN_INVALID")
    receipt, receipt_hash = _read_json(
        install_root / "update-receipt.json", install_root,
    )
    if receipt_hash != expected_receipt_sha256:
        _deny("LEGACY_RECEIPT_DRIFT")
    if (receipt.get("result") != "HOST_UPDATE_BLOCKED"
            or receipt.get("source_commit") != LEGACY_SOURCE_COMMIT
            or receipt.get("rollback_attempted") is not True
            or receipt.get("rollback_succeeded") is not False):
        _deny("LEGACY_RECEIPT_STATE_INVALID")
    backup_value = receipt.get("backup_dir")
    if type(backup_value) is not str:
        _deny("LEGACY_BACKUP_REFERENCE_INVALID")
    backup = Path(backup_value)
    if (not BACKUP_RE.fullmatch(backup.name)
            or backup.resolve(strict=True).parent != install_root.resolve()):
        _deny("LEGACY_BACKUP_REFERENCE_INVALID")
    manifest, manifest_hash = _read_json(
        backup / "rollback-manifest.json", install_root,
    )
    if manifest_hash != expected_manifest_sha256:
        _deny("LEGACY_MANIFEST_DRIFT")
    if (manifest.get("version") != 1 or "metadata_contract" in manifest
            or manifest.get("install_root") != str(install_root.resolve())):
        _deny("LEGACY_MANIFEST_HEADER_INVALID")
    entries = manifest.get("entries")
    if type(entries) is not dict or set(entries) != set(bootstrap.RUNTIME_MAP.values()):
        _deny("LEGACY_MANAGED_SET_INVALID")

    stable: list[tuple[Path, str, int, int, dict[str, Any]]] = []
    absent = 0
    for source, destination in bootstrap.RUNTIME_MAP.items():
        item = entries[destination]
        if type(item) is not dict or item.get("source") != source or type(
            item.get("existed")
        ) is not bool:
            _deny("LEGACY_ENTRY_INVALID")
        _expected_hash(item.get("desired_sha256"), "LEGACY_ENTRY_INVALID")
        if type(item.get("desired_size")) is not int or item["desired_size"] < 0:
            _deny("LEGACY_ENTRY_INVALID")
        current = bootstrap._managed_target(install_root, destination)
        stored = backup / "files" / destination
        if item["existed"] is False:
            if current.exists() or current.is_symlink() or stored.exists() or stored.is_symlink():
                _deny("LEGACY_UNEXPECTED_FILE")
            absent += 1
            continue
        _expected_hash(item.get("previous_sha256"), "LEGACY_ENTRY_INVALID")
        size, mode = item.get("previous_size"), item.get("previous_mode")
        if (type(size) is not int or not 0 <= size <= MAX_FILE_BYTES
                or type(mode) is not int or not 0 <= mode <= 0o7777):
            _deny("LEGACY_ENTRY_INVALID")
        previous_bytes = _confined_regular(stored, install_root, max_bytes=MAX_FILE_BYTES)
        current_bytes = _confined_regular(current, install_root, max_bytes=MAX_FILE_BYTES)
        if (_digest(previous_bytes) != item["previous_sha256"]
                or _digest(current_bytes) != item["previous_sha256"]
                or len(previous_bytes) != size or len(current_bytes) != size
                or stat.S_IMODE(stored.stat().st_mode) != mode
                or stat.S_IMODE(current.stat().st_mode) != mode):
            _deny("LEGACY_CONTENT_OR_MODE_DRIFT")
        try:
            metadata = bootstrap._windows_persisted_file_state(current)
            bootstrap._validate_windows_persisted_state(metadata, destination)
        except (OSError, ValueError, bootstrap.HostBootstrapError):
            _deny("LEGACY_CURRENT_METADATA_UNAVAILABLE")
        stable.append((current, item["previous_sha256"], size, mode, metadata))

    # Verify again after enumerating all seven files; do not claim atomicity
    # against a process operating outside the updater's exclusive lock.
    for target, digest, size, mode, metadata in stable:
        payload = _confined_regular(target, install_root, max_bytes=MAX_FILE_BYTES)
        if (_digest(payload) != digest or len(payload) != size
                or stat.S_IMODE(target.stat().st_mode) != mode):
            _deny("LEGACY_STATE_CHANGED_DURING_READ")
        try:
            observed = bootstrap._windows_persisted_file_state(target)
            if not bootstrap._windows_persisted_state_equivalent(metadata, observed):
                _deny("LEGACY_METADATA_CHANGED_DURING_READ")
        except (OSError, KeyError, ValueError, bootstrap.HostBootstrapError):
            _deny("LEGACY_CURRENT_METADATA_UNAVAILABLE")

    return {
        "result": "LEGACY_CURRENT_STATE_VERIFIED",
        "source_commit": LEGACY_SOURCE_COMMIT,
        "receipt_sha256": receipt_hash,
        "manifest_sha256": manifest_hash,
        "existing_file_count": len(stable),
        "absent_file_count": absent,
        "current_windows_metadata_readable": True,
        "historical_windows_metadata_verified": False,
        "new_v2_snapshot_created": False,
        "managed_runtime_changed": False,
        "installation_authorized": False,
    }

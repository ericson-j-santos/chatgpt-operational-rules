#!/usr/bin/env python3
"""Attest four fixed Noteri gateway incident files and rollback without mutations.

Stage-only; never recovers a journal, updates a service, or authorizes an install.
Run only from a validated Session Launcher worktree via Command Gateway Risk1.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import stat
from pathlib import Path

ROOT = Path(r"C:\dev\chatgpt-command-gateway")
TX = "50227f5817504ca4b328a962a076d673"
RULES = "58dcd4200f412a866278027ba9e433439a03f3b3"
PREVIOUS = "11e2e86d0283e83247d8058d05bbabe3dd7cc2c2"
BACKUP_NAME = "backup-20261112-195830-8f2a4b2c5e754c0bba7b87602873e4ed"
CONFIRM = "ATTEST-NOTERI-JOURNAL-50227F-READONLY"
MANAGED = (
    "bin/command_gateway.py", "bin/host_health_check.py",
    "bin/runner_version_preflight.py", "bin/session_bootstrap.py",
    "bin/session_launcher.py", "bin/session_preflight.py", "config/policy.json",
)


class Blocked(Exception):
    pass


def read_stable(path: Path, root: Path, maximum: int = 4 * 1024 * 1024) -> bytes:
    """Read a confined regular file twice and refuse Windows reparse points."""
    if not path.is_relative_to(root) or path == root:
        raise Blocked("PATH_OUTSIDE_PINNED_ROOT")
    for node in (root, *list(path.relative_to(root).parents)[:-1]):
        current = root if node == root else root / node
        try:
            st = current.lstat()
        except OSError as exc:
            raise Blocked("PARENT_UNREADABLE") from exc
        if not stat.S_ISDIR(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:
            raise Blocked("PARENT_REDIRECT_OR_INVALID")
    try:
        before = path.lstat()
    except OSError as exc:
        raise Blocked("FILE_UNREADABLE") from exc
    if (not stat.S_ISREG(before.st_mode) or
            getattr(before, "st_file_attributes", 0) & 0x400 or
            not 0 < before.st_size <= maximum):
        raise Blocked("FILE_REDIRECT_OR_SIZE")
    first, second = path.read_bytes(), path.read_bytes()
    after = path.lstat()
    if (first != second or len(first) != before.st_size or
            (before.st_size, before.st_mtime_ns, before.st_ino) !=
            (after.st_size, after.st_mtime_ns, after.st_ino)):
        raise Blocked("FILE_CHANGED_DURING_READ")
    return first


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def doc(path: Path, root: Path) -> tuple[dict, str]:
    raw = read_stable(path, root)
    try:
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_keys)
    except (ValueError, UnicodeError) as exc:
        raise Blocked("INVALID_JSON") from exc
    if not isinstance(obj, dict):
        raise Blocked("JSON_OBJECT_REQUIRED")
    return obj, digest(raw)


def unique_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("DUPLICATE_KEY")
        out[key] = value
    return out


def inspect(root: Path) -> dict:
    backup = root / BACKUP_NAME
    paths = {
        "journal": root / "update-transaction.json",
        "failure": root / f"update-recovery-{TX}.json",
        "primary": root / "update-receipt.json",
        "manifest": backup / "rollback-manifest.json",
    }
    records, hashes = {}, {}
    for name, path in paths.items():
        records[name], hashes[name] = doc(path, root)
    journal, failure, primary, manifest = (records[k] for k in paths)
    if (journal.get("transaction_id") != TX or journal.get("source_commit") != RULES
            or journal.get("backup_dir") != str(backup)):
        raise Blocked("JOURNAL_BINDING_INVALID")
    if (failure.get("transaction_id") != TX or failure.get("source_commit") != RULES
            or failure.get("backup_dir") != str(backup)
            or failure.get("result") != "HOST_UPDATE_BLOCKED"
            or failure.get("rollback_succeeded") is not True
            or failure.get("reconciliation", {}).get("previous_state") is not True):
        raise Blocked("FAILURE_BINDING_INVALID")
    if (primary.get("source_commit") != PREVIOUS or
            primary.get("rules_version") != "1.6.20" or
            primary.get("result") != "HOST_UPDATE_OK"):
        raise Blocked("PRIMARY_RECEIPT_INVALID")
    entries = manifest.get("entries")
    if (manifest.get("version") != 2 or
            manifest.get("metadata_contract") != "windows-owner-group-dacl-ads-v1" or
            manifest.get("install_root") != str(root) or
            not isinstance(entries, dict) or set(entries) != set(MANAGED)):
        raise Blocked("MANIFEST_INVALID")
    verified = 0
    for rel in MANAGED:
        entry = entries[rel]
        if entry.get("existed") is not True:
            raise Blocked("MANAGED_ENTRY_MISSING")
        expected = entry.get("previous_sha256")
        size = entry.get("previous_size")
        if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise Blocked("MANAGED_SHA_INVALID")
        for file in (root / rel, backup / "files" / rel):
            data = read_stable(file, root)
            if digest(data) != expected or len(data) != size:
                raise Blocked("ROLLBACK_FILE_DIVERGENT")
        verified += 1
    # Detect writes between the first and last observations.
    for name, path in paths.items():
        if digest(read_stable(path, root)) != hashes[name]:
            raise Blocked("EVIDENCE_CHANGED")
    return {"result": "NOTERI_JOURNAL_50227F_READONLY_ATTESTED",
            "transaction_id": TX, "source_commit": RULES,
            "hashes": hashes, "previous_files_content_verified": verified,
            "windows_security_metadata_verified": False,
            "recovery_authorized": False, "state_changed": False}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    try:
        if (os.name != "nt" or socket.gethostname().casefold() != "noteri"
                or args.confirm != CONFIRM):
            raise Blocked("HOST_OR_CONFIRM_INVALID")
        print(json.dumps(inspect(ROOT), sort_keys=True))
        return 0
    except (Blocked, OSError, TypeError, ValueError) as exc:
        error = str(exc) if isinstance(exc, Blocked) else "READONLY_INVENTORY_UNAVAILABLE"
        print(json.dumps({"result": "NOTERI_JOURNAL_50227F_READONLY_BLOCKED",
                          "reason": error, "state_changed": False}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

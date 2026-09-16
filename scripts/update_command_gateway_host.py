#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import install_command_gateway_host as bootstrap

EXIT_HOST_UPDATE = 28


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, payload: dict) -> None:
    bootstrap.atomic_write(
        path,
        (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
    )


def restore_backup(install_root: Path, backup_dir: Path | None) -> None:
    if backup_dir is None or not backup_dir.is_dir():
        return
    for dest in bootstrap.RUNTIME_MAP.values():
        previous = backup_dir / dest
        if previous.is_file():
            bootstrap.atomic_write(install_root / dest, previous.read_bytes())


def main() -> int:
    parser = argparse.ArgumentParser(description="Atualização governada do runtime Command Gateway")
    parser.add_argument("--commit", required=True, help="SHA completo aprovado do repositório canônico")
    parser.add_argument("--expected-self-sha256", required=True, help="SHA-256 esperado deste updater")
    parser.add_argument("--install-root", type=Path, default=None)
    parser.add_argument("--work-root", type=Path, default=Path(r"C:\dev\chatgpt-workers"))
    ns = parser.parse_args()

    install_root = ns.install_root or bootstrap.default_install_root()
    receipt_path = install_root / "update-receipt.json"
    backup_dir: Path | None = None
    commit: str | None = None
    try:
        expected = ns.expected_self_sha256.strip().lower()
        actual = sha256_file(Path(__file__).resolve())
        if len(expected) != 64 or actual != expected:
            raise bootstrap.HostBootstrapError("SHA-256 do updater divergente")
        commit = bootstrap.validate_commit(ns.commit)
        existing_gateway = install_root / "bin" / "command_gateway.py"
        existing_policy = install_root / "config" / "policy.json"
        if not existing_gateway.is_file() or not existing_policy.is_file():
            raise bootstrap.HostBootstrapError("runtime existente não comprovado; use bootstrap inicial")

        with tempfile.TemporaryDirectory(prefix="reqsys-host-update-") as tmp:
            bundle = Path(tmp)
            bootstrap.download_bundle(commit, bundle)
            result = bootstrap.install_bundle(bundle, install_root, ns.work_root, commit)
            raw_backup = result.get("backup_dir")
            backup_dir = Path(raw_backup) if raw_backup else None

        git_executable, git_source = bootstrap.resolve_git(install_root)
        validation_repo, validation_head = bootstrap.prepare_validation_repo(
            ns.work_root, commit, git_executable=git_executable
        )
        receipt = {
            "result": "HOST_UPDATE_OK",
            "source_commit": commit,
            "rules_version": result.get("rules_version"),
            "updated_at": utc_now(),
            "host": result.get("host"),
            "install_root": str(install_root),
            "backup_dir": str(backup_dir) if backup_dir else None,
            "git_source": git_source,
            "validation_repo": str(validation_repo),
            "validation_head": validation_head,
            "files": result.get("files", {}),
        }
        atomic_json(receipt_path, receipt)
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        try:
            restore_backup(install_root, backup_dir)
        except Exception:
            pass
        blocked = {
            "result": "HOST_UPDATE_BLOCKED",
            "gateway_exit_code": EXIT_HOST_UPDATE,
            "source_commit": commit,
            "blocked_at": utc_now(),
            "error": str(exc),
            "rollback_attempted": backup_dir is not None,
        }
        try:
            atomic_json(receipt_path, blocked)
        except Exception:
            pass
        print(json.dumps(blocked, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return EXIT_HOST_UPDATE


if __name__ == "__main__":
    raise SystemExit(main())

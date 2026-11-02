#!/usr/bin/env python3
"""Inventario independente e Risk1 do estado do Gateway Noteri, sem mutacoes.

Entradas CLI fechadas: confirmacao, correlation ID e SHA-256 desta fonte.
Nao usa Gateway, subprocesso, shell, rede, escrita nem caminhos fornecidos.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import stat
import sys
from pathlib import Path
from typing import Callable

HOST = "noteri"
CONFIRM = "INSPECT_NOTERI_GATEWAY_STATE_READONLY"
REPARSE_POINT = 0x400
CORRELATION = re.compile(r"[A-Za-z0-9._:-]{1,100}\Z")


def targets_from_environment(localappdata: str | None) -> tuple[tuple[str, Path, str], ...]:
    if not localappdata:
        raise ValueError("LOCALAPPDATA_MISSING")
    base = Path(localappdata)
    normalized = str(base).replace("/", "\\").rstrip("\\").casefold()
    if not base.is_absolute() or not normalized.endswith("\\appdata\\local"):
        raise ValueError("LOCALAPPDATA_NOT_QUALIFIED")
    state = base / "ReqSys" / "CommandGateway"
    gateway = Path(r"C:\dev\chatgpt-command-gateway")
    return (
        ("reqsys_repository", Path(r"C:\dev\reqsys-v2-enterprise-real"), "directory"),
        ("gateway_root", gateway, "directory"),
        ("gateway_launcher", gateway / "bin" / "session_launcher.py", "file"),
        ("gateway_policy", gateway / "config" / "policy.json", "file"),
        ("state_root", state, "directory"),
        ("state_sessions", state / "sessions", "directory"),
        ("state_snapshots", state / "snapshots", "directory"),
        ("state_locks", state / "locks", "directory"),
    )


def inspect_path(
    target: Path, expected: str, *, stat_fn: Callable = os.lstat,
) -> tuple[dict, tuple]:
    """Inspeciona cada componente usando lstat (Windows no-follow)."""
    if not target.is_absolute() or expected not in {"file", "directory"}:
        return {"status": "INVALID_TARGET", "component_index": 0}, ("INVALID_TARGET",)
    chain = list(reversed(target.parents)) + [target]
    fingerprint: list[tuple] = []
    for index, component in enumerate(chain):
        try:
            observed = stat_fn(component)
        except FileNotFoundError:
            return {"status": "MISSING", "component_index": index}, ("MISSING", index)
        except NotADirectoryError:
            return {"status": "NOT_DIRECTORY", "component_index": index}, ("NOT_DIRECTORY", index)
        except OSError as exc:
            code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
            if code == 267:  # ERROR_DIRECTORY no Windows
                return {"status": "NOT_DIRECTORY", "component_index": index}, ("NOT_DIRECTORY", index)
            code = code if type(code) is int and 0 <= code <= 65535 else 0
            return (
                {"status": "OS_ERROR", "component_index": index, "os_code": code},
                ("OS_ERROR", index, code),
            )
        attrs = int(getattr(observed, "st_file_attributes", 0))
        signature = (
            int(observed.st_dev), int(observed.st_ino), int(observed.st_mode),
            attrs, int(getattr(observed, "st_size", 0)),
            int(getattr(observed, "st_mtime_ns", 0)),
        )
        fingerprint.append(signature)
        if stat.S_ISLNK(observed.st_mode) or attrs & REPARSE_POINT:
            return {"status": "REPARSE", "component_index": index}, ("REPARSE", index, tuple(fingerprint))
        kind = expected if index == len(chain) - 1 else "directory"
        if kind == "directory" and not stat.S_ISDIR(observed.st_mode):
            return {"status": "NOT_DIRECTORY", "component_index": index}, ("NOT_DIRECTORY", index, tuple(fingerprint))
        if kind == "file" and not stat.S_ISREG(observed.st_mode):
            return {"status": "NOT_FILE", "component_index": index}, ("NOT_FILE", index, tuple(fingerprint))
    return {"status": "PRESENT", "component_index": len(chain) - 1}, ("PRESENT", tuple(fingerprint))


def inspect_twice(
    targets: tuple[tuple[str, Path, str], ...], *, stat_fn: Callable = os.lstat,
) -> dict:
    if not targets or len({name for name, _, _ in targets}) != len(targets):
        return {"result": "BLOCKED_TARGET_SET", "state_changed": False, "mutated": False}
    first = [(name, *inspect_path(path, kind, stat_fn=stat_fn)) for name, path, kind in targets]
    second = [(name, *inspect_path(path, kind, stat_fn=stat_fn)) for name, path, kind in targets]
    if first != second:
        return {"result": "BLOCKED_OBSERVATION_CHANGED", "state_changed": True, "mutated": False}
    entries = [{"target": name, **summary} for name, summary, _ in second]
    ok = all(item["status"] == "PRESENT" for item in entries)
    return {
        "result": "INSPECTION_OK" if ok else "INSPECTION_BLOCKED",
        "state_changed": False, "mutated": False, "entries": entries,
    }


def authorized(host: str, platform: str, bytecode_disabled: bool, confirm: str) -> bool:
    return (
        host.casefold() == HOST and platform == "nt"
        and bytecode_disabled is True and confirm == CONFIRM
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--correlation-id", required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    result = {"schema": 1, "result": "INSPECTION_BLOCKED", "state_changed": False, "mutated": False}
    if (
        not CORRELATION.fullmatch(args.correlation_id)
        or not re.fullmatch(r"[0-9a-f]{64}", args.expected_sha256)
        or not authorized(socket.gethostname(), os.name, sys.dont_write_bytecode, args.confirm)
    ):
        result["reason"] = "ADMISSION_BLOCKED"
    elif hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != args.expected_sha256:
        result["reason"] = "SOURCE_DIGEST_MISMATCH"
    else:
        try:
            result.update(inspect_twice(targets_from_environment(os.environ.get("LOCALAPPDATA"))))
        except (OSError, ValueError):
            result["reason"] = "ENVIRONMENT_BLOCKED"
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result["result"] == "INSPECTION_OK" else 28


if __name__ == "__main__":
    raise SystemExit(main())

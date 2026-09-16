#!/usr/bin/env python3
"""Guarded remediation for the Windows Remote Desktop Commander launcher."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

ALLOWED_NAME = "start-remote-desktop-commander.cmd"
EXPECTED_OLD_SHA256 = "d86b2eafaa6b679727f1db28d41f8489e7c2ae1fcb3529da750cf52ff5e02dd6"
MARKER = "REM RDC_LAUNCHER_V2_GOVERNED"
NEW_CONTENT = r'''@echo off
setlocal EnableExtensions
''' + MARKER + r'''
title Remote Desktop Commander - Automation

set "RDC_LOG=%USERPROFILE%\RemoteDesktopCommander.log"
set "RDC_MAX_RETRIES=5"
set "RDC_RETRY_SECONDS=15"
set /a RDC_ATTEMPT=0

:run
set /a RDC_ATTEMPT+=1
echo [%date% %time%] Iniciando Remote Desktop Commander tentativa %RDC_ATTEMPT%/%RDC_MAX_RETRIES%... >> "%RDC_LOG%"

REM npx no Windows resolve para npx.cmd. CALL e obrigatorio para devolver controle a este batch.
call npx --yes @wonderwhy-er/desktop-commander@latest remote >> "%RDC_LOG%" 2>&1
set "RDC_EXIT=%ERRORLEVEL%"

echo [%date% %time%] Remote Desktop Commander terminou com codigo %RDC_EXIT%. >> "%RDC_LOG%"
if "%RDC_EXIT%"=="0" exit /b 0
if %RDC_ATTEMPT% GEQ %RDC_MAX_RETRIES% exit /b %RDC_EXIT%

echo [%date% %time%] Reinicio controlado em %RDC_RETRY_SECONDS%s. >> "%RDC_LOG%"
timeout /t %RDC_RETRY_SECONDS% /nobreak >nul
goto :run
'''.replace("\n", "\r\n")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def validate_target(raw: str) -> Path:
    path = Path(raw).resolve()
    if path.name.casefold() != ALLOWED_NAME:
        raise ValueError("launcher basename not authorized")
    return path


def inspect(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = path.read_bytes()
    text = payload.decode("utf-8-sig", errors="replace")
    return {
        "path": str(path),
        "sha256": sha256_bytes(payload),
        "size": len(payload),
        "already_fixed": MARKER in text,
        "contains_call_npx": "call npx " in text.casefold(),
    }


def apply(path: Path, expected_sha256: str) -> dict[str, object]:
    before = inspect(path)
    if before["already_fixed"]:
        return {"result": "already_fixed", "before": before, "after": before, "backup": None}
    if str(before["sha256"]).lower() != expected_sha256.lower():
        raise ValueError(
            f"launcher hash changed: expected={expected_sha256.lower()} actual={before['sha256']}"
        )
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(path.name + f".bak-{timestamp}")
    shutil.copy2(path, backup)
    temp = path.with_name(path.name + ".tmp")
    temp.write_bytes(NEW_CONTENT.encode("utf-8"))
    os.replace(temp, path)
    after = inspect(path)
    if not after["already_fixed"] or not after["contains_call_npx"]:
        shutil.copy2(backup, path)
        raise RuntimeError("post-write validation failed; backup restored")
    return {"result": "fixed", "before": before, "after": after, "backup": str(backup)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Guarded RDC launcher remediation")
    parser.add_argument("--launcher", required=True)
    parser.add_argument("--expected-sha256", default=EXPECTED_OLD_SHA256)
    parser.add_argument("--apply", action="store_true")
    ns = parser.parse_args()
    try:
        target = validate_target(ns.launcher)
        result = apply(target, ns.expected_sha256) if ns.apply else {"result": "dry_run", "state": inspect(target)}
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"result": "blocked", "error": str(exc)}, ensure_ascii=True, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

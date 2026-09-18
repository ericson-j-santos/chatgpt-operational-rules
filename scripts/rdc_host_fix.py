#!/usr/bin/env python3
"""Guarded remediation for the Windows Remote Desktop Commander launcher."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ALLOWED_NAME = "start-remote-desktop-commander.cmd"
LEGACY_V1_SHA256 = "d86b2eafaa6b679727f1db28d41f8489e7c2ae1fcb3529da750cf52ff5e02dd6"
V2_SHA256 = "001cb4f595d305ea757c4b251b7fefaee9b0df7732f7a373ae742c7a6f850767"
V2_MARKER = "REM RDC_LAUNCHER_V2_GOVERNED"
V3_MARKER = "REM RDC_LAUNCHER_V3_RESILIENT"
PINNED_PACKAGE = "@wonderwhy-er/desktop-commander@0.2.51"
NEW_CONTENT = r'''@echo off
setlocal EnableExtensions
''' + V3_MARKER + r'''
title Remote Desktop Commander - Resilient Automation

set "RDC_LOG=%USERPROFILE%\RemoteDesktopCommander.log"
set "RDC_RETRY_SECONDS=15"
set "RDC_PACKAGE=@wonderwhy-er/desktop-commander@0.2.51"

if /I "%~1"=="--self-test" goto :selftest

:run
echo [%date% %time%] RDC supervisor starting %RDC_PACKAGE%... >> "%RDC_LOG%"

REM CALL is mandatory for npx.cmd so control returns to this supervisor.
call npx --yes %RDC_PACKAGE% remote >> "%RDC_LOG%" 2>&1
set "RDC_EXIT=%ERRORLEVEL%"

echo [%date% %time%] RDC child exited code=%RDC_EXIT%; restart in %RDC_RETRY_SECONDS%s. >> "%RDC_LOG%"
timeout /t %RDC_RETRY_SECONDS% /nobreak >nul
goto :run

:selftest
call npx --yes %RDC_PACKAGE% --version >> "%RDC_LOG%" 2>&1
set "RDC_EXIT=%ERRORLEVEL%"
echo [%date% %time%] RDC_SELF_TEST package=%RDC_PACKAGE% code=%RDC_EXIT%. >> "%RDC_LOG%"
exit /b %RDC_EXIT%
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
    marker = "v3" if V3_MARKER in text else "v2" if V2_MARKER in text else "legacy"
    return {
        "path": str(path),
        "sha256": sha256_bytes(payload),
        "size": len(payload),
        "marker": marker,
        "already_fixed": marker == "v3",
        "contains_call_npx": "call npx " in text.casefold(),
        "contains_self_test": "rdc_self_test" in text.casefold(),
        "contains_pinned_package": PINNED_PACKAGE.casefold() in text.casefold(),
        "contains_watchdog_loop": "goto :run" in text.casefold(),
    }


def apply(path: Path, expected_sha256: list[str]) -> dict[str, object]:
    before = inspect(path)
    if before["already_fixed"]:
        return {"result": "already_fixed", "before": before, "after": before, "backup": None}
    allowed = {value.lower() for value in expected_sha256 if value}
    actual = str(before["sha256"]).lower()
    if actual not in allowed:
        raise ValueError(f"launcher hash changed: allowed={sorted(allowed)} actual={actual}")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(path.name + f".bak-{timestamp}")
    shutil.copy2(path, backup)
    temp = path.with_name(path.name + ".tmp")
    temp.write_bytes(NEW_CONTENT.encode("utf-8"))
    os.replace(temp, path)
    after = inspect(path)
    required = (
        after["already_fixed"],
        after["contains_call_npx"],
        after["contains_self_test"],
        after["contains_pinned_package"],
        after["contains_watchdog_loop"],
    )
    if not all(required):
        shutil.copy2(backup, path)
        raise RuntimeError("post-write validation failed; backup restored")
    return {"result": "fixed", "before": before, "after": after, "backup": str(backup)}


def self_test(path: Path) -> dict[str, object]:
    state = inspect(path)
    if not state["already_fixed"] or not state["contains_self_test"]:
        raise ValueError("launcher does not contain resilient governed self-test")
    comspec = os.environ.get("COMSPEC", "cmd.exe")
    completed = subprocess.run(
        [comspec, "/d", "/c", str(path), "--self-test"],
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        timeout=120,
        check=False,
    )
    return {
        "result": "self_test_passed" if completed.returncode == 0 else "self_test_failed",
        "exit_code": completed.returncode,
        "state": state,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Guarded resilient RDC launcher remediation")
    parser.add_argument("--launcher", required=True)
    parser.add_argument(
        "--expected-sha256",
        action="append",
        default=[],
        help="Allowed source hash; may be supplied more than once.",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    ns = parser.parse_args()
    try:
        target = validate_target(ns.launcher)
        allowed = ns.expected_sha256 or [LEGACY_V1_SHA256, V2_SHA256]
        if ns.apply and ns.self_test:
            raise ValueError("choose only one action: --apply or --self-test")
        if ns.apply:
            result = apply(target, allowed)
        elif ns.self_test:
            result = self_test(target)
        else:
            result = {"result": "dry_run", "state": inspect(target), "allowed_source_hashes": allowed}
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0 if result.get("result") != "self_test_failed" else 3
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"result": "blocked", "error": str(exc)}, ensure_ascii=True, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
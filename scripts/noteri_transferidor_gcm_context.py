#!/usr/bin/env python3
"""TRANSF-008: noninteractive, sanitized GitHub GCM/account-context inspection.

DEV / Noteri only; requires an already admitted SHA-bound Session Launcher and
Command Gateway. Does not access credential values, log identities, or open UI.
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import socket
import subprocess
from ctypes import wintypes
from typing import Any, Callable

from noteri_transferidor_gcm_oauth import no_secret_environment, profile_ok
from noteri_transferidor_console_context import windows_sessions


def _current_and_console_users() -> tuple[str, str] | None:
    """Return names only to this process for comparison; never serialize them."""
    if os.name != "nt":
        return None
    active = windows_sessions()
    if active is None or active[0] == 0 or active[0] != active[1]:
        return None
    try:
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        username = advapi.GetUserNameW
        username.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        username.restype = wintypes.BOOL
        size = wintypes.DWORD(512)
        holder = ctypes.create_unicode_buffer(size.value)
        if not username(holder, ctypes.byref(size)) or not holder.value:
            return None
        wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
        query = wts.WTSQuerySessionInformationW
        query.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                          ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
        query.restype = wintypes.BOOL
        release = wts.WTSFreeMemory
        release.argtypes = [ctypes.c_void_p]
        release.restype = None
        ptr = ctypes.c_void_p()
        length = wintypes.DWORD(0)
        # WTSUserName = 5, current terminal server handle = null.
        if not query(None, wintypes.DWORD(active[0]), wintypes.DWORD(5),
                     ctypes.byref(ptr), ctypes.byref(length)) or not ptr.value:
            return None
        try:
            if length.value < 2 or length.value > 1024 or length.value % 2:
                return None
            console_name = ctypes.wstring_at(ptr.value, length.value // 2).rstrip("\0").strip()
        finally:
            release(ptr)
        process_name = holder.value.strip()
        if not console_name or not process_name:
            return None
        return process_name, console_name
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def probe(*, platform: str, hostname: str, localappdata: str | None,
          git: str | None, environment: dict[str, str],
          identity_reader: Callable[[], tuple[str, str] | None] = _current_and_console_users,
          runner: Callable[..., Any] = subprocess.run) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": "1", "task_id": "TRANSF-008", "host": "Noteri",
        "result": "BLOCKED_HOST_OR_PROFILE", "process_console_user_matches": False,
        "identity_comparison_available": False, "gcm_account_list_has_output": False,
        "gcm_login_started": False, "credentials_read": False,
        "secret_value_exposed": False, "state_changed": False,
    }
    if not profile_ok(os_name=platform, hostname=hostname, localappdata=localappdata):
        return result
    if not git:
        result["result"] = "BLOCKED_GIT_UNAVAILABLE"
        return result
    try:
        pair = identity_reader()
    except (OSError, ValueError, AttributeError, TypeError):
        pair = None
    if (isinstance(pair, tuple) and len(pair) == 2 and
            all(isinstance(x, str) and 0 < len(x) <= 512 for x in pair)):
        result["identity_comparison_available"] = True
        result["process_console_user_matches"] = pair[0].casefold() == pair[1].casefold()
    env = no_secret_environment(environment)
    env["GCM_INTERACTIVE"] = "never"
    try:
        proc = runner([git, "credential-manager", "github", "list", "--url", "https://github.com", "--no-ui"],
                      stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                      stderr=subprocess.PIPE, text=True, encoding="utf-8",
                      errors="replace", timeout=15, check=False, shell=False,
                      env=env)
    except (OSError, subprocess.TimeoutExpired):
        result["result"] = "GCM_ACCOUNT_LIST_UNAVAILABLE"
        return result
    if proc.returncode != 0:
        result["result"] = "GCM_ACCOUNT_LIST_FAILED"
    elif not isinstance(proc.stdout, str) or len(proc.stdout) > 32768:
        result["result"] = "GCM_ACCOUNT_LIST_INVALID"
    elif not proc.stdout.strip():
        result["result"] = "GCM_ACCOUNT_LIST_EMPTY"
    else:
        # Nonempty output does not prove usable credentials or repo access.
        result["result"] = "GCM_ACCOUNT_LIST_NONEMPTY_UNVERIFIED"
        result["gcm_account_list_has_output"] = True
    return result


def main() -> int:
    result = probe(platform=os.name, hostname=socket.gethostname(),
                   localappdata=os.environ.get("LOCALAPPDATA"),
                   git=shutil.which("git"), environment=os.environ)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    # Only validated host/profile + an attempted read-only inventory can pass.
    return 28 if result["result"].startswith("BLOCKED_") else 0


if __name__ == "__main__":
    raise SystemExit(main())

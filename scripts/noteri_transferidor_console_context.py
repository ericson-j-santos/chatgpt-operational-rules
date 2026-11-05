#!/usr/bin/env python3
"""TRANSF-008: diagnose Noteri desktop session without UI, credentials or state changes.

Intended to run only via Session Launcher + Command Gateway, risk=1, SHA-pinned.
The result is a *candidate* for interactive consent, never proof that a browser opened.
"""
from __future__ import annotations

import ctypes
import json
import os
import socket
from ctypes import wintypes
from typing import Callable

from noteri_transferidor_gcm_oauth import profile_ok


def windows_sessions() -> tuple[int, int] | None:
    """Read kernel session IDs; never query usernames, windows or credentials."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    active_api = kernel.WTSGetActiveConsoleSessionId
    active_api.argtypes = []
    active_api.restype = wintypes.DWORD
    active = int(active_api())
    if active == 0xFFFFFFFF:
        return None
    session_id = wintypes.DWORD(0)
    process_api = kernel.ProcessIdToSessionId
    process_api.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    process_api.restype = wintypes.BOOL
    if not process_api(wintypes.DWORD(os.getpid()), ctypes.byref(session_id)):
        return None
    return int(active), int(session_id.value)


def probe(*, platform: str, host: str, localappdata: str | None,
          reader: Callable[[], tuple[int, int] | None] = windows_sessions) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": "1", "task_id": "TRANSF-008", "host": "Noteri",
        "result": "BLOCKED_HOST_OR_PROFILE", "same_active_console_session": False,
        "interactive_consent_proven": False, "browser_launched": False,
        "secret_value_exposed": False, "state_changed": False,
    }
    if not profile_ok(os_name=platform, hostname=host, localappdata=localappdata):
        return result
    try:
        sessions = reader()
    except (OSError, ValueError, AttributeError):
        sessions = None
    if sessions is None or len(sessions) != 2 or any(type(x) is not int or x < 0 or x > 0xFFFFFFFF for x in sessions):
        result["result"] = "SESSION_QUERY_UNAVAILABLE"
        return result
    active, current = sessions
    if active == 0xFFFFFFFF:
        result["result"] = "SESSION_QUERY_UNAVAILABLE"
    elif current == 0:
        result["result"] = "PROCESS_SESSION_ZERO"
    elif active == 0:
        result["result"] = "ACTIVE_CONSOLE_SESSION_ZERO"
    elif current != active:
        result["result"] = "PROCESS_NOT_IN_ACTIVE_CONSOLE"
    else:
        result["result"] = "ACTIVE_CONSOLE_CONTEXT_CANDIDATE"
        result["same_active_console_session"] = True
    return result


def main() -> int:
    output = probe(platform=os.name, host=socket.gethostname(),
                   localappdata=os.environ.get("LOCALAPPDATA"))
    print(json.dumps(output, ensure_ascii=True, sort_keys=True))
    # A host/profile error or an API failure is not an admissible diagnosis.
    return 28 if str(output["result"]).startswith(("BLOCKED_", "SESSION_QUERY_")) else 0


if __name__ == "__main__":
    raise SystemExit(main())

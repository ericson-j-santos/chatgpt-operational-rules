#!/usr/bin/env python3
"""TRANSF-008: diagnose WinCred session persistence without identities/secrets.

Noteri DEV only. Runs under an already admitted Session Launcher / Gateway
Risk1; never invokes Git, GCM, account APIs, OAuth, UI or credential reads.
"""
from __future__ import annotations

import ctypes
import json
import os
import socket
from ctypes import wintypes
from typing import Any, Callable

from noteri_transferidor_gcm_oauth import profile_ok
from noteri_transferidor_console_context import windows_sessions

CRED_TYPE_MAXIMUM = 8
CRED_TYPE_GENERIC = 1
PERSISTENCE = {0: "NONE", 1: "SESSION", 2: "LOCAL_MACHINE", 3: "ENTERPRISE"}


def credential_persistence(*, platform: str, dll_loader: Callable[..., Any] | None = None) -> str:
    """Read only the session's maximum generic credential persistence class."""
    if platform != "nt":
        return "UNAVAILABLE"
    try:
        loader = dll_loader if dll_loader is not None else ctypes.WinDLL
        api = loader("advapi32", use_last_error=True)
        fn = api.CredGetSessionTypes
        fn.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        fn.restype = wintypes.BOOL
        levels = (wintypes.DWORD * CRED_TYPE_MAXIMUM)()
        if not fn(CRED_TYPE_MAXIMUM, levels):
            return "UNAVAILABLE"
        return PERSISTENCE.get(int(levels[CRED_TYPE_GENERIC]), "UNAVAILABLE")
    except (OSError, AttributeError, TypeError, ValueError):
        return "UNAVAILABLE"


def probe(*, platform: str, hostname: str, localappdata: str | None,
          session_reader: Callable[[], tuple[int, int] | None] = windows_sessions,
          persistence_reader: Callable[[], str] | None = None) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": "1", "task_id": "TRANSF-008", "host": "Noteri",
        "result": "BLOCKED_HOST_OR_PROFILE", "same_active_console_session": False,
        "credential_persistence": "NOT_CHECKED", "credential_store_candidate": False,
        "source_head_verified": False, "oauth_browser_requested": False,
        "credentials_read": False, "secret_value_exposed": False,
        "state_changed": False,
    }
    if not profile_ok(os_name=platform, hostname=hostname, localappdata=localappdata):
        return result
    try:
        pair = session_reader()
    except (OSError, AttributeError, TypeError, ValueError):
        pair = None
    if (not isinstance(pair, tuple) or len(pair) != 2 or
            any(type(v) is not int or v < 0 or v >= 0xFFFFFFFF for v in pair)):
        result["result"] = "SESSION_QUERY_UNAVAILABLE"
        return result
    active, current = pair
    if current == 0 or active == 0 or current != active:
        result["result"] = "PROCESS_NOT_IN_ACTIVE_CONSOLE"
        return result
    result["same_active_console_session"] = True
    try:
        persistence = (persistence_reader or
                       (lambda: credential_persistence(platform=platform)))()
    except (OSError, AttributeError, TypeError, ValueError):
        persistence = "UNAVAILABLE"
    if persistence not in (*PERSISTENCE.values(), "UNAVAILABLE"):
        persistence = "UNAVAILABLE"
    result["credential_persistence"] = persistence
    if persistence == "UNAVAILABLE":
        result["result"] = "CREDENTIAL_PERSISTENCE_UNKNOWN"
    elif persistence == "NONE":
        result["result"] = "CREDENTIAL_PERSISTENCE_NONE"
    else:
        result["result"] = "CREDENTIAL_PERSISTENCE_CANDIDATE"
        result["credential_store_candidate"] = True
    return result


def main() -> int:
    result = probe(platform=os.name, hostname=socket.gethostname(),
                   localappdata=os.environ.get("LOCALAPPDATA"))
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    # Exit 0 means only that a non-promoting diagnostic was classified.
    return 28 if result["result"] == "BLOCKED_HOST_OR_PROFILE" else 0


if __name__ == "__main__":
    raise SystemExit(main())

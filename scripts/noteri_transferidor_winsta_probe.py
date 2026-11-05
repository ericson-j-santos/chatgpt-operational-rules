#!/usr/bin/env python3
"""TRANSF-008: diagnóstica a visibilidade da estação de janelas do Noteri sem GUI.

Somente leitura. Um resultado candidato não comprova abertura de navegador ou
consentimento OAuth. Executar exclusivamente por Session Launcher / Gateway Risk1.
"""
from __future__ import annotations

import ctypes
import json
import os
import socket
from ctypes import wintypes
from typing import Callable

from noteri_transferidor_console_context import windows_sessions
from noteri_transferidor_gcm_oauth import profile_ok

UOI_FLAGS = 1
WSF_VISIBLE = 1


class UserObjectFlags(ctypes.Structure):
    _fields_ = [
        ("inherit", wintypes.BOOL),
        ("reserved", wintypes.BOOL),
        ("flags", wintypes.DWORD),
    ]


def window_station_is_visible() -> bool | None:
    """Consulta exclusivamente WSF_VISIBLE da estação do processo atual."""
    if os.name != "nt":
        return None
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        get_station = user32.GetProcessWindowStation
        get_station.argtypes = []
        get_station.restype = wintypes.HANDLE
        station = get_station()
        if not station:
            return None
        get_info = user32.GetUserObjectInformationW
        get_info.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ]
        get_info.restype = wintypes.BOOL
        data = UserObjectFlags()
        needed = wintypes.DWORD(0)
        if not get_info(station, UOI_FLAGS, ctypes.byref(data),
                        ctypes.sizeof(data), ctypes.byref(needed)):
            return None
        if needed.value > ctypes.sizeof(data):
            return None
        return bool(data.flags & WSF_VISIBLE)
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def probe(*, platform: str, host: str, localappdata: str | None,
          sessions_reader: Callable[[], tuple[int, int] | None] = windows_sessions,
          station_reader: Callable[[], bool | None] = window_station_is_visible,
          ) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": "1", "task_id": "TRANSF-008", "host": "Noteri",
        "result": "BLOCKED_HOST_OR_PROFILE", "same_active_console_session": False,
        "visible_window_station": False, "interactive_desktop_proven": False,
        "browser_launched": False, "secret_value_exposed": False,
        "state_changed": False,
    }
    if not profile_ok(os_name=platform, hostname=host, localappdata=localappdata):
        return result
    try:
        sessions = sessions_reader()
    except (OSError, ValueError, TypeError, AttributeError):
        sessions = None
    if (not isinstance(sessions, tuple) or len(sessions) != 2 or
            any(type(v) is not int or v <= 0 or v >= 0xFFFFFFFF for v in sessions)):
        result["result"] = "SESSION_QUERY_UNAVAILABLE"
        return result
    active, current = sessions
    if active != current:
        result["result"] = "PROCESS_NOT_IN_ACTIVE_CONSOLE"
        return result
    result["same_active_console_session"] = True
    try:
        visible = station_reader()
    except (OSError, ValueError, TypeError, AttributeError):
        visible = None
    if type(visible) is not bool:
        result["result"] = "WINDOW_STATION_QUERY_UNAVAILABLE"
    elif visible is False:
        result["result"] = "WINDOW_STATION_NOT_VISIBLE"
    else:
        result["result"] = "VISIBLE_WINDOW_STATION_CANDIDATE"
        result["visible_window_station"] = True
    return result


def main() -> int:
    receipt = probe(platform=os.name, host=socket.gethostname(),
                    localappdata=os.environ.get("LOCALAPPDATA"))
    print(json.dumps(receipt, ensure_ascii=True, sort_keys=True))
    # Diagnóstico válido não implica que o OAuth esteja autorizado.
    return 28 if str(receipt["result"]).startswith(("BLOCKED_", "SESSION_QUERY_", "WINDOW_STATION_QUERY_")) else 0


if __name__ == "__main__":
    raise SystemExit(main())

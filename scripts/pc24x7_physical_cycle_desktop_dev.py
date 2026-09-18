from __future__ import annotations

import ctypes
import json
import socket
import time
from ctypes import wintypes

TARGET = "DESKTOP-PDQK954"
TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_QUERY = 0x0008
SE_PRIVILEGE_ENABLED = 0x00000002
ERROR_NOT_ALL_ASSIGNED = 1300
SHTDN_REASON_MAJOR_APPLICATION = 0x00040000
SHTDN_REASON_MINOR_MAINTENANCE = 0x00000001
SHTDN_REASON_FLAG_PLANNED = 0x80000000


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]


def windows_apis():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.LookupPrivilegeValueW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.POINTER(LUID),
    ]
    advapi32.LookupPrivilegeValueW.restype = wintypes.BOOL
    advapi32.AdjustTokenPrivileges.argtypes = [
        wintypes.HANDLE,
        wintypes.BOOL,
        ctypes.POINTER(TOKEN_PRIVILEGES),
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    advapi32.AdjustTokenPrivileges.restype = wintypes.BOOL
    advapi32.InitiateSystemShutdownExW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    advapi32.InitiateSystemShutdownExW.restype = wintypes.BOOL
    return kernel32, advapi32


def enable_shutdown_privilege() -> None:
    kernel32, advapi32 = windows_apis()
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(),
        TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
        ctypes.byref(token),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        luid = LUID()
        if not advapi32.LookupPrivilegeValueW(None, "SeShutdownPrivilege", ctypes.byref(luid)):
            raise ctypes.WinError(ctypes.get_last_error())
        privileges = TOKEN_PRIVILEGES(
            1,
            (LUID_AND_ATTRIBUTES(luid, SE_PRIVILEGE_ENABLED),),
        )
        ctypes.set_last_error(0)
        if not advapi32.AdjustTokenPrivileges(
            token,
            False,
            ctypes.byref(privileges),
            0,
            None,
            None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        error = ctypes.get_last_error()
        if error == ERROR_NOT_ALL_ASSIGNED:
            raise PermissionError("SeShutdownPrivilege is not assigned to this token")
        if error:
            raise ctypes.WinError(error)
    finally:
        kernel32.CloseHandle(token)


def schedule_restart(delay_seconds: int) -> None:
    _, advapi32 = windows_apis()
    reason = (
        SHTDN_REASON_MAJOR_APPLICATION
        | SHTDN_REASON_MINOR_MAINTENANCE
        | SHTDN_REASON_FLAG_PLANNED
    )
    ok = advapi32.InitiateSystemShutdownExW(
        None,
        "Authorized PC24x7 physical failover/failback E2E",
        delay_seconds,
        False,
        True,
        reason,
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())


def main() -> int:
    host = socket.gethostname()
    if host.casefold() != TARGET.casefold():
        raise SystemExit(f"refusing physical cycle on unexpected host: {host}")
    delay_seconds = 10
    enable_shutdown_privilege()
    schedule_restart(delay_seconds)
    print(json.dumps({
        "status": "scheduled",
        "host": host,
        "delay_seconds": delay_seconds,
        "api": "InitiateSystemShutdownExW",
        "privilege": "SeShutdownPrivilege",
        "reason": "authorized_pc24x7_physical_failover_failback_e2e",
        "observed_at_epoch": time.time(),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

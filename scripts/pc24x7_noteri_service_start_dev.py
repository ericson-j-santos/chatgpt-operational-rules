from __future__ import annotations

import ctypes
import json
import time
from ctypes import wintypes

SERVICE_NAME = "TodoGlobal24x7NoteriHA"
SC_MANAGER_CONNECT = 0x0001
SERVICE_QUERY_STATUS = 0x0004
SERVICE_START = 0x0010
SC_STATUS_PROCESS_INFO = 0
SERVICE_STOPPED = 0x00000001
SERVICE_START_PENDING = 0x00000002
SERVICE_RUNNING = 0x00000004
ERROR_SERVICE_ALREADY_RUNNING = 1056


class SERVICE_STATUS_PROCESS(ctypes.Structure):
    _fields_ = [
        ("dwServiceType", wintypes.DWORD),
        ("dwCurrentState", wintypes.DWORD),
        ("dwControlsAccepted", wintypes.DWORD),
        ("dwWin32ExitCode", wintypes.DWORD),
        ("dwServiceSpecificExitCode", wintypes.DWORD),
        ("dwCheckPoint", wintypes.DWORD),
        ("dwWaitHint", wintypes.DWORD),
        ("dwProcessId", wintypes.DWORD),
        ("dwServiceFlags", wintypes.DWORD),
    ]


advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
advapi32.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
advapi32.OpenSCManagerW.restype = wintypes.HANDLE
advapi32.OpenServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD]
advapi32.OpenServiceW.restype = wintypes.HANDLE
advapi32.StartServiceW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.LPCWSTR)]
advapi32.StartServiceW.restype = wintypes.BOOL
advapi32.QueryServiceStatusEx.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(ctypes.c_ubyte),
    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
]
advapi32.QueryServiceStatusEx.restype = wintypes.BOOL
advapi32.CloseServiceHandle.argtypes = [wintypes.HANDLE]
advapi32.CloseServiceHandle.restype = wintypes.BOOL


def query(service):
    status = SERVICE_STATUS_PROCESS()
    needed = wintypes.DWORD()
    ok = advapi32.QueryServiceStatusEx(
        service,
        SC_STATUS_PROCESS_INFO,
        ctypes.cast(ctypes.byref(status), ctypes.POINTER(ctypes.c_ubyte)),
        ctypes.sizeof(status),
        ctypes.byref(needed),
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    return status


def main() -> int:
    scm = advapi32.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
    if not scm:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        service = advapi32.OpenServiceW(scm, SERVICE_NAME, SERVICE_QUERY_STATUS | SERVICE_START)
        if not service:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            before = query(service)
            if before.dwCurrentState != SERVICE_RUNNING:
                if not advapi32.StartServiceW(service, 0, None):
                    error = ctypes.get_last_error()
                    if error != ERROR_SERVICE_ALREADY_RUNNING:
                        raise ctypes.WinError(error)
            deadline = time.monotonic() + 45
            last = before
            while time.monotonic() < deadline:
                last = query(service)
                if last.dwCurrentState == SERVICE_RUNNING:
                    break
                time.sleep(1)
            print(json.dumps({
                "status": "running" if last.dwCurrentState == SERVICE_RUNNING else "not_running",
                "before_state": int(before.dwCurrentState),
                "after_state": int(last.dwCurrentState),
                "pid": int(last.dwProcessId),
                "win32_exit": int(last.dwWin32ExitCode),
                "service_exit": int(last.dwServiceSpecificExitCode),
            }, sort_keys=True))
            return 0 if last.dwCurrentState == SERVICE_RUNNING else 2
        finally:
            advapi32.CloseServiceHandle(service)
    finally:
        advapi32.CloseServiceHandle(scm)


if __name__ == "__main__":
    raise SystemExit(main())

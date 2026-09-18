from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "scripts" / "pc24x7_physical_cycle_noteri_dev.py"
SEE_MASK_NOCLOSEPROCESS = 0x00000040
SW_SHOWNORMAL = 1
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258


class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


def is_admin() -> bool:
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def request_elevation(timeout_seconds: int) -> dict[str, object]:
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = sys.executable
    info.lpParameters = f'"{TARGET}"'
    info.lpDirectory = str(ROOT)
    info.nShow = SW_SHOWNORMAL

    started = time.time()
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        return {
            "status": "elevation_not_started",
            "win32_error": ctypes.get_last_error(),
            "elapsed_seconds": round(time.time() - started, 1),
        }
    try:
        wait = kernel32.WaitForSingleObject(info.hProcess, max(1, min(timeout_seconds, 300)) * 1000)
        if wait == WAIT_TIMEOUT:
            return {"status": "uac_or_reboot_pending", "elapsed_seconds": round(time.time()-started, 1)}
        if wait != WAIT_OBJECT_0:
            return {"status": "wait_failed", "wait_result": int(wait)}
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code)):
            return {"status": "exit_code_unavailable", "win32_error": ctypes.get_last_error()}
        return {"status": "completed", "exit_code": int(code.value), "elapsed_seconds": round(time.time()-started, 1)}
    finally:
        if info.hProcess:
            kernel32.CloseHandle(info.hProcess)


def main() -> int:
    if is_admin():
        cp = subprocess.run([sys.executable, str(TARGET)], cwd=ROOT, check=False)
        result = {"status": "already_elevated", "exit_code": cp.returncode}
    else:
        result = request_elevation(60)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") in {"completed", "already_elevated"} and result.get("exit_code") == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())

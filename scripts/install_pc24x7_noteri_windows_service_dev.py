from __future__ import annotations

import ctypes
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import winreg
from ctypes import wintypes
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
SERVICE_NAME = "TodoGlobal24x7NoteriHA"
DISPLAY_NAME = "Todo Global 24x7 - Noteri HA Worker"
BASE = Path(os.environ["LOCALAPPDATA"]) / "ReqSys" / "TodoGlobal24x7Noteri"
RUNTIME_ENV = BASE / "runtime.env"
LEGACY_PID = BASE / "host-worker-ha" / "worker.pid"
VENV_PYTHON = BASE / "host-worker" / "venv" / "Scripts" / "python.exe"
PROGRAM_DATA = Path(os.environ.get("ProgramData", r"C:\ProgramData"))
INSTALL_ROOT = PROGRAM_DATA / "ReqSys" / "TodoGlobal24x7NoteriService"
PACKAGE_ROOT = INSTALL_ROOT / "package"
SERVICE_HOST = INSTALL_ROOT / "service_host.py"
CONFIG_FILE = INSTALL_ROOT / "service-config.json"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "TodoGlobal24x7NoteriHA"
SERVICE_WORKER_ID = "continuation-worker-Noteri-ha-service"

SC_MANAGER_CONNECT = 0x0001
SC_MANAGER_CREATE_SERVICE = 0x0002
SERVICE_QUERY_STATUS = 0x0004
SERVICE_CHANGE_CONFIG = 0x0002
SERVICE_START = 0x0010
SERVICE_STOP = 0x0020
SERVICE_WIN32_OWN_PROCESS = 0x00000010
SERVICE_AUTO_START = 0x00000002
SERVICE_ERROR_NORMAL = 0x00000001
SERVICE_NO_CHANGE = 0xFFFFFFFF
SC_STATUS_PROCESS_INFO = 0
SERVICE_RUNNING = 0x00000004
SERVICE_STOPPED = 0x00000001
ERROR_SERVICE_DOES_NOT_EXIST = 1060
ERROR_SERVICE_ALREADY_RUNNING = 1056

FILES = (
    "continuation_worker_ha.py",
    "continuation_worker.py",
    "host_router.py",
    "ha_database_guard.py",
)


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
advapi32.CreateServiceW.argtypes = [
    wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR,
    wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD), wintypes.LPCWSTR,
    wintypes.LPCWSTR, wintypes.LPCWSTR,
]
advapi32.CreateServiceW.restype = wintypes.HANDLE
advapi32.OpenServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD]
advapi32.OpenServiceW.restype = wintypes.HANDLE
advapi32.ChangeServiceConfigW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD),
    wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
]
advapi32.ChangeServiceConfigW.restype = wintypes.BOOL
advapi32.StartServiceW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.LPCWSTR)]
advapi32.StartServiceW.restype = wintypes.BOOL
advapi32.QueryServiceStatusEx.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(ctypes.c_ubyte),
    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
]
advapi32.QueryServiceStatusEx.restype = wintypes.BOOL
advapi32.CloseServiceHandle.argtypes = [wintypes.HANDLE]
advapi32.CloseServiceHandle.restype = wintypes.BOOL


def read_runtime_env() -> dict[str, str]:
    if not RUNTIME_ENV.is_file():
        raise RuntimeError("Noteri runtime.env missing")
    values: dict[str, str] = {}
    for raw in RUNTIME_ENV.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    dsn = values.get("DATABASE_URL", "")
    parsed = urlparse(dsn)
    if not (parsed.hostname or "").endswith(".neon.tech"):
        raise RuntimeError("Noteri service refuses non-Neon DATABASE_URL")
    if (parsed.path or "").lstrip("/") != "todo_global_bus_dev_ha":
        raise RuntimeError("Noteri service refuses non-canonical database")
    return values


def materialize_package() -> None:
    INSTALL_ROOT.mkdir(parents=True, exist_ok=True)
    scripts_dst = PACKAGE_ROOT / "scripts"
    scripts_dst.mkdir(parents=True, exist_ok=True)
    (scripts_dst / "__init__.py").write_text("", encoding="utf-8")
    for name in FILES:
        shutil.copy2(ROOT / "scripts" / name, scripts_dst / name)
    shutil.copy2(ROOT / "scripts" / "pc24x7_noteri_windows_service.py", SERVICE_HOST)
    CONFIG_FILE.write_text(
        json.dumps({
            "python_exe": str(VENV_PYTHON),
            "package_root": str(PACKAGE_ROOT),
            "runtime_env": str(RUNTIME_ENV),
        }, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def open_scm() -> wintypes.HANDLE:
    handle = advapi32.OpenSCManagerW(None, None, SC_MANAGER_CONNECT | SC_MANAGER_CREATE_SERVICE)
    if not handle:
        error = ctypes.get_last_error()
        if error == 5:
            raise PermissionError("Windows service installation requires elevated local administrator rights")
        raise ctypes.WinError(error)
    return handle


def service_binary_path() -> str:
    return f'"{VENV_PYTHON}" "{SERVICE_HOST}"'


def create_or_update_service() -> wintypes.HANDLE:
    scm = open_scm()
    try:
        access = SERVICE_QUERY_STATUS | SERVICE_CHANGE_CONFIG | SERVICE_START | SERVICE_STOP
        service = advapi32.OpenServiceW(scm, SERVICE_NAME, access)
        if service:
            if not advapi32.ChangeServiceConfigW(
                service,
                SERVICE_NO_CHANGE,
                SERVICE_AUTO_START,
                SERVICE_NO_CHANGE,
                service_binary_path(),
                None, None, None, None, None,
                DISPLAY_NAME,
            ):
                error = ctypes.get_last_error()
                advapi32.CloseServiceHandle(service)
                raise ctypes.WinError(error)
            return service

        error = ctypes.get_last_error()
        if error != ERROR_SERVICE_DOES_NOT_EXIST:
            raise ctypes.WinError(error)

        service = advapi32.CreateServiceW(
            scm,
            SERVICE_NAME,
            DISPLAY_NAME,
            access,
            SERVICE_WIN32_OWN_PROCESS,
            SERVICE_AUTO_START,
            SERVICE_ERROR_NORMAL,
            service_binary_path(),
            None, None, None,
            None,
            None,
        )
        if not service:
            error = ctypes.get_last_error()
            if error == 5:
                raise PermissionError("Windows service creation requires elevated local administrator rights")
            raise ctypes.WinError(error)
        return service
    finally:
        advapi32.CloseServiceHandle(scm)


def query_service(service: wintypes.HANDLE) -> SERVICE_STATUS_PROCESS:
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


def ensure_service_running(service: wintypes.HANDLE) -> SERVICE_STATUS_PROCESS:
    status = query_service(service)
    if status.dwCurrentState == SERVICE_STOPPED:
        if not advapi32.StartServiceW(service, 0, None):
            error = ctypes.get_last_error()
            if error != ERROR_SERVICE_ALREADY_RUNNING:
                raise ctypes.WinError(error)
    deadline = time.monotonic() + 45
    last = status
    while time.monotonic() < deadline:
        last = query_service(service)
        if last.dwCurrentState == SERVICE_RUNNING:
            return last
        if last.dwCurrentState == SERVICE_STOPPED and last.dwWin32ExitCode:
            raise RuntimeError(
                f"Noteri service stopped during startup win32_exit={last.dwWin32ExitCode} "
                f"service_exit={last.dwServiceSpecificExitCode}"
            )
        time.sleep(1)
    raise RuntimeError(f"Noteri service did not reach RUNNING, state={last.dwCurrentState}")


def heartbeat_age(values: dict[str, str]) -> int:
    env = os.environ.copy()
    env["DATABASE_URL"] = values["DATABASE_URL"]
    code = (
        "import os,psycopg;"
        "c=psycopg.connect(os.environ['DATABASE_URL']);"
        "r=c.execute(\"SELECT round(extract(epoch from clock_timestamp()-heartbeat_at)) "
        "FROM todo_bus.worker_heartbeat WHERE worker_id='continuation-worker-Noteri-ha-service'\").fetchone();"
        "print(-1 if r is None else int(r[0]));c.close()"
    )
    result = subprocess.run(
        [str(VENV_PYTHON), "-c", code],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=15,
    )
    if result.returncode != 0:
        return -1
    try:
        return int(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return -1


def wait_service_heartbeat(values: dict[str, str]) -> int:
    deadline = time.monotonic() + 60
    last = -1
    while time.monotonic() < deadline:
        last = heartbeat_age(values)
        if 0 <= last <= 15:
            return last
        time.sleep(2)
    raise RuntimeError(f"service heartbeat not observed, age={last}")


def process_alive(pid: int) -> bool:
    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return result.returncode == 0 and str(pid) in result.stdout


def retire_legacy_worker() -> bool:
    if not LEGACY_PID.is_file():
        return True
    try:
        pid = int(LEGACY_PID.read_text(encoding="ascii").strip())
    except ValueError:
        LEGACY_PID.unlink(missing_ok=True)
        return True
    if pid and process_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and process_alive(pid):
            time.sleep(0.5)
        if process_alive(pid):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False)
    LEGACY_PID.unlink(missing_ok=True)
    return not process_alive(pid)


def remove_logon_fallback() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass
        return True
    except OSError:
        return False


def main() -> int:
    if socket.gethostname().casefold() != "noteri":
        raise SystemExit("refusing Noteri service install on unexpected host")
    values = read_runtime_env()
    if not VENV_PYTHON.is_file():
        raise SystemExit("Noteri worker venv missing")

    materialize_package()
    service = create_or_update_service()
    try:
        status = ensure_service_running(service)
        age = wait_service_heartbeat(values)
    finally:
        advapi32.CloseServiceHandle(service)

    legacy_stopped = retire_legacy_worker()
    fallback_removed = remove_logon_fallback()
    if not legacy_stopped or not fallback_removed:
        raise RuntimeError(
            f"service is healthy but legacy cleanup failed legacy_stopped={legacy_stopped} "
            f"fallback_removed={fallback_removed}"
        )

    print(json.dumps({
        "status": "ok",
        "service_name": SERVICE_NAME,
        "start_type": "automatic",
        "service_account": "LocalSystem",
        "service_pid": int(status.dwProcessId),
        "service_heartbeat_age_seconds": age,
        "worker_id": SERVICE_WORKER_ID,
        "legacy_worker_stopped": legacy_stopped,
        "logon_fallback_removed": fallback_removed,
        "database": "todo_global_bus_dev_ha",
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

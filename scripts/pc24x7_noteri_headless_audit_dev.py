from __future__ import annotations

import ctypes
import json
import os
import socket
import subprocess
import winreg
from ctypes import wintypes
from pathlib import Path
from urllib.parse import urlparse

SERVICE_NAME = "TodoGlobal24x7NoteriHA"
BASE = Path(os.environ["LOCALAPPDATA"]) / "ReqSys" / "TodoGlobal24x7Noteri"
RUNTIME_ENV = BASE / "runtime.env"
PID_FILE = BASE / "host-worker-ha" / "worker.pid"
VENV_PYTHON = BASE / "host-worker" / "venv" / "Scripts" / "python.exe"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "TodoGlobal24x7NoteriHA"
SC_MANAGER_CONNECT = 0x0001
SERVICE_QUERY_STATUS = 0x0004
ERROR_SERVICE_DOES_NOT_EXIST = 1060

advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
advapi32.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
advapi32.OpenSCManagerW.restype = wintypes.HANDLE
advapi32.OpenServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD]
advapi32.OpenServiceW.restype = wintypes.HANDLE
advapi32.CloseServiceHandle.argtypes = [wintypes.HANDLE]
advapi32.CloseServiceHandle.restype = wintypes.BOOL


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in RUNTIME_ENV.read_text(encoding="utf-8").splitlines():
        if "=" not in raw or raw.lstrip().startswith("#"):
            continue
        key, value = raw.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def process_alive(pid: int) -> bool:
    cp = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    return cp.returncode == 0 and str(pid) in cp.stdout


def fallback_present() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE)
        return bool(str(value).strip())
    except OSError:
        return False


def service_exists() -> bool:
    scm = advapi32.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
    if not scm:
        return False
    try:
        service = advapi32.OpenServiceW(scm, SERVICE_NAME, SERVICE_QUERY_STATUS)
        if service:
            advapi32.CloseServiceHandle(service)
            return True
        return ctypes.get_last_error() != ERROR_SERVICE_DOES_NOT_EXIST
    finally:
        advapi32.CloseServiceHandle(scm)


def heartbeat_age(dsn: str, worker_id: str) -> int:
    env = os.environ.copy()
    env["DATABASE_URL"] = dsn
    code = (
        "import os,psycopg;"
        "c=psycopg.connect(os.environ['DATABASE_URL']);"
        f"r=c.execute(\"SELECT round(extract(epoch from clock_timestamp()-heartbeat_at)) "
        f"FROM todo_bus.worker_heartbeat WHERE worker_id='{worker_id}'\").fetchone();"
        "print(-1 if r is None else int(r[0]));c.close()"
    )
    cp = subprocess.run(
        [str(VENV_PYTHON), "-c", code],
        env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=15, check=False,
    )
    if cp.returncode != 0:
        return -1
    try:
        return int(cp.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return -1


def bridge_reachable() -> bool:
    try:
        with socket.create_connection(("192.168.1.45", 15432), timeout=2):
            return True
    except OSError:
        return False


def main() -> int:
    values = load_env()
    dsn = values.get("DATABASE_URL", "")
    parsed = urlparse(dsn)
    pid = 0
    if PID_FILE.is_file():
        try:
            pid = int(PID_FILE.read_text(encoding="ascii").strip())
        except ValueError:
            pid = 0

    result = {
        "database_host_is_neon": bool(parsed.hostname and parsed.hostname.endswith(".neon.tech")),
        "database_name": (parsed.path or "").lstrip("/"),
        "legacy_worker_pid_present": bool(pid),
        "legacy_worker_alive": bool(pid and process_alive(pid)),
        "legacy_worker_heartbeat_age_seconds": heartbeat_age(dsn, "continuation-worker-Noteri-ha"),
        "service_exists": service_exists(),
        "service_worker_heartbeat_age_seconds": heartbeat_age(dsn, "continuation-worker-Noteri-ha-service"),
        "logon_fallback_present": fallback_present(),
        "desktop_legacy_bridge_reachable": bridge_reachable(),
    }
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

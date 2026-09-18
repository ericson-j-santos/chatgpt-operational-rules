from __future__ import annotations

import ctypes
import json
import os
import subprocess
import threading
import time
from ctypes import wintypes
from pathlib import Path

SERVICE_NAME = "TodoGlobal24x7NoteriHA"
SERVICE_WIN32_OWN_PROCESS = 0x00000010
SERVICE_STOPPED = 0x00000001
SERVICE_START_PENDING = 0x00000002
SERVICE_STOP_PENDING = 0x00000003
SERVICE_RUNNING = 0x00000004
SERVICE_ACCEPT_STOP = 0x00000001
SERVICE_ACCEPT_SHUTDOWN = 0x00000004
SERVICE_CONTROL_STOP = 0x00000001
SERVICE_CONTROL_SHUTDOWN = 0x00000005

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "service-config.json"
LOG = ROOT / "worker-service.log"
CHILD_PID = ROOT / "worker-service.pid"

_stop_event = threading.Event()
_status_handle = None
_child: subprocess.Popen | None = None


class SERVICE_STATUS(ctypes.Structure):
    _fields_ = [
        ("dwServiceType", wintypes.DWORD),
        ("dwCurrentState", wintypes.DWORD),
        ("dwControlsAccepted", wintypes.DWORD),
        ("dwWin32ExitCode", wintypes.DWORD),
        ("dwServiceSpecificExitCode", wintypes.DWORD),
        ("dwCheckPoint", wintypes.DWORD),
        ("dwWaitHint", wintypes.DWORD),
    ]


HANDLER_EX = ctypes.WINFUNCTYPE(
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p
)
SERVICE_MAIN = ctypes.WINFUNCTYPE(None, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR))


class SERVICE_TABLE_ENTRY(ctypes.Structure):
    _fields_ = [("lpServiceName", wintypes.LPWSTR), ("lpServiceProc", SERVICE_MAIN)]


advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
advapi32.RegisterServiceCtrlHandlerExW.argtypes = [wintypes.LPCWSTR, HANDLER_EX, ctypes.c_void_p]
advapi32.RegisterServiceCtrlHandlerExW.restype = wintypes.HANDLE
advapi32.SetServiceStatus.argtypes = [wintypes.HANDLE, ctypes.POINTER(SERVICE_STATUS)]
advapi32.SetServiceStatus.restype = wintypes.BOOL
advapi32.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(SERVICE_TABLE_ENTRY)]
advapi32.StartServiceCtrlDispatcherW.restype = wintypes.BOOL


def set_status(state: int, *, win32_exit: int = 0, wait_hint: int = 0) -> None:
    if not _status_handle:
        return
    accepted = 0
    if state == SERVICE_RUNNING:
        accepted = SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN
    status = SERVICE_STATUS(
        SERVICE_WIN32_OWN_PROCESS,
        state,
        accepted,
        win32_exit,
        0,
        0,
        wait_hint,
    )
    if not advapi32.SetServiceStatus(_status_handle, ctypes.byref(status)):
        raise ctypes.WinError(ctypes.get_last_error())


@HANDLER_EX
def handler(control, event_type, event_data, context):
    if control in (SERVICE_CONTROL_STOP, SERVICE_CONTROL_SHUTDOWN):
        _stop_event.set()
        try:
            set_status(SERVICE_STOP_PENDING, wait_hint=15000)
        except Exception:
            pass
    return 0


def load_config() -> dict[str, str]:
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    required = ("python_exe", "package_root", "runtime_env")
    missing = [key for key in required if not str(data.get(key, "")).strip()]
    if missing:
        raise RuntimeError("service config missing: " + ",".join(missing))
    return {str(k): str(v) for k, v in data.items()}


def load_runtime_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def start_worker() -> subprocess.Popen:
    config = load_config()
    python_exe = Path(config["python_exe"])
    package_root = Path(config["package_root"])
    runtime_env = Path(config["runtime_env"])
    if not python_exe.is_file() or not package_root.is_dir() or not runtime_env.is_file():
        raise RuntimeError("service dependency missing")

    values = load_runtime_env(runtime_env)
    env = os.environ.copy()
    env.update(values)
    env.update({
        "WORKER_NODE_ID": "Noteri",
        "WORKER_ID": "continuation-worker-Noteri-ha-service",
        "WORKER_CAPABILITIES": "continuation",
        "ROUTER_PRIMARY_NODE": "DESKTOP-PDQK954",
        "ROUTER_SECONDARY_NODE": "Noteri",
        "ROUTER_HEARTBEAT_TTL_SECONDS": "20",
        "ROUTER_PRIMARY_STABILITY_SECONDS": "15",
        "HA_MODE": "enabled",
        "HA_DATABASE_REQUIRED_HOST_SUFFIX": ".neon.tech",
        "HA_DATABASE_REQUIRED_NAME": "todo_global_bus_dev_ha",
        "PYTHONPATH": str(package_root),
    })
    log = LOG.open("a", encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(
        [str(python_exe), "-m", "scripts.continuation_worker_ha", "--interval-seconds", "5"],
        cwd=package_root,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        creationflags=flags,
        close_fds=True,
    )
    CHILD_PID.write_text(str(proc.pid), encoding="ascii")
    return proc


def stop_worker(proc: subprocess.Popen | None) -> None:
    CHILD_PID.unlink(missing_ok=True)
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


@SERVICE_MAIN
def service_main(argc, argv):
    global _status_handle, _child
    _status_handle = advapi32.RegisterServiceCtrlHandlerExW(SERVICE_NAME, handler, None)
    if not _status_handle:
        return
    try:
        set_status(SERVICE_START_PENDING, wait_hint=20000)
        _child = start_worker()
        time.sleep(2)
        if _child.poll() is not None:
            raise RuntimeError(f"worker exited rc={_child.returncode}")
        set_status(SERVICE_RUNNING)
        while not _stop_event.wait(1):
            if _child.poll() is not None:
                raise RuntimeError(f"worker exited unexpectedly rc={_child.returncode}")
        stop_worker(_child)
        set_status(SERVICE_STOPPED)
    except Exception:
        stop_worker(_child)
        try:
            set_status(SERVICE_STOPPED, win32_exit=1)
        except Exception:
            pass


def run_service() -> int:
    table = (SERVICE_TABLE_ENTRY * 2)()
    table[0].lpServiceName = SERVICE_NAME
    table[0].lpServiceProc = service_main
    table[1].lpServiceName = None
    table[1].lpServiceProc = SERVICE_MAIN()
    if not advapi32.StartServiceCtrlDispatcherW(table):
        raise ctypes.WinError(ctypes.get_last_error())
    return 0


if __name__ == "__main__":
    raise SystemExit(run_service())

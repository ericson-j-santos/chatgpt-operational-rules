from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
BASE = Path.home() / "AppData" / "Local" / "ReqSys" / "TodoGlobal24x7Noteri"
STATE = BASE / "host-worker-ha"
ENV_FILE = BASE / "runtime.env"
VENV = BASE / "host-worker" / "venv"
PID_FILE = STATE / "worker.pid"
LOG_FILE = STATE / "worker.log"


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


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


def main() -> int:
    if not ENV_FILE.is_file():
        raise SystemExit("Noteri runtime.env missing")
    STATE.mkdir(parents=True, exist_ok=True)
    values = load_env()
    database_url = values.get("DATABASE_URL", "")
    parsed = urlparse(database_url)
    if not parsed.hostname:
        raise SystemExit("DATABASE_URL host invalid")
    port = parsed.port or 5432
    with socket.create_connection((parsed.hostname, port), timeout=5):
        pass

    if PID_FILE.is_file():
        try:
            existing = int(PID_FILE.read_text(encoding="ascii").strip())
        except ValueError:
            existing = 0
        if existing and process_alive(existing):
            print(json.dumps({"status": "already_running", "pid": existing, "node_id": "Noteri"}, sort_keys=True))
            return 0

    python_exe = VENV / "Scripts" / "python.exe"
    if not python_exe.is_file():
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
        subprocess.run(
            [str(python_exe), "-m", "pip", "install", "--disable-pip-version-check", "psycopg[binary,pool]==3.3.5"],
            check=True,
            stdout=subprocess.DEVNULL,
        )

    env = os.environ.copy()
    env.update(values)
    env.update({
        "WORKER_NODE_ID": "Noteri",
        "WORKER_ID": "continuation-worker-Noteri-ha",
        "WORKER_CAPABILITIES": "continuation",
        "ROUTER_PRIMARY_NODE": "DESKTOP-PDQK954",
        "ROUTER_SECONDARY_NODE": "Noteri",
        "ROUTER_HEARTBEAT_TTL_SECONDS": "20",
        "ROUTER_PRIMARY_STABILITY_SECONDS": "15",
        "PYTHONPATH": str(ROOT),
    })
    log = LOG_FILE.open("a", encoding="utf-8")
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(
        [str(python_exe), "-m", "scripts.continuation_worker_ha", "--interval-seconds", "5"],
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        creationflags=flags,
        close_fds=True,
    )
    PID_FILE.write_text(str(proc.pid), encoding="ascii")
    time.sleep(3)
    if proc.poll() is not None:
        raise SystemExit(f"Noteri HA worker exited rc={proc.returncode}")
    print(json.dumps({"status": "running", "pid": proc.pid, "node_id": "Noteri", "worker_id": "continuation-worker-Noteri-ha"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

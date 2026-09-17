from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

BASE = Path.home() / "AppData" / "Local" / "ReqSys" / "TodoGlobal24x7Noteri"
ENV_FILE = BASE / "runtime.env"
LEGACY_STATE = BASE / "host-worker"
PID_FILE = LEGACY_STATE / "worker.pid"
VENV_PYTHON = LEGACY_STATE / "venv" / "Scripts" / "python.exe"


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def alive(pid: int) -> bool:
    result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", check=False)
    return result.returncode == 0 and str(pid) in result.stdout


def safety_check(values: dict[str, str]) -> tuple[int, int]:
    env = os.environ.copy()
    env.update(values)
    code = (
        "import os,psycopg; c=psycopg.connect(os.environ['DATABASE_URL']); "
        "r=c.execute(\"SELECT round(extract(epoch from clock_timestamp()-heartbeat_at)) FROM todo_bus.worker_heartbeat WHERE worker_id='continuation-worker-Noteri-ha'\").fetchone(); "
        "p=c.execute(\"SELECT count(*) FROM todo_bus.continuation_requests q JOIN todo_bus.host_route_decisions d ON d.correlation_id=q.correlation_id WHERE q.state='PROCESSING' AND d.node_id='Noteri'\").fetchone(); "
        "print(f'{int(r[0]) if r else 999999}|{int(p[0])}')"
    )
    result = subprocess.run([str(VENV_PYTHON), "-c", code], env=env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", check=True)
    age, processing = result.stdout.strip().split("|", 1)
    return int(age), int(processing)


def main() -> int:
    if not PID_FILE.exists():
        print("legacy_worker_absent")
        return 0
    try:
        pid = int(PID_FILE.read_text(encoding="ascii").strip())
    except ValueError:
        raise SystemExit("legacy worker pid invalid")
    if not alive(pid):
        PID_FILE.unlink(missing_ok=True)
        print("legacy_worker_already_stopped")
        return 0
    values = load_env()
    age, processing = safety_check(values)
    if age > 15:
        raise SystemExit(f"HA heartbeat stale: {age}s")
    if processing != 0:
        raise SystemExit(f"Noteri has {processing} processing continuation(s)")
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        if not alive(pid):
            PID_FILE.unlink(missing_ok=True)
            print("legacy_worker_retired")
            return 0
        time.sleep(0.5)
    raise SystemExit("legacy worker did not stop gracefully")


if __name__ == "__main__":
    raise SystemExit(main())

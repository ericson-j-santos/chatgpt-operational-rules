from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
BASE = Path(os.environ["LOCALAPPDATA"]) / "ReqSys" / "TodoGlobal24x7Noteri"
RUNTIME_ENV = BASE / "runtime.env"
SECRET_ENV = BASE / "neon-ha.env"
BACKUP_ENV = BASE / "runtime.env.pre-neon-ha"
PID_FILE = BASE / "host-worker-ha" / "worker.pid"
EXPECTED_DB = "todo_global_bus_dev_ha"


def process_alive(pid: int) -> bool:
    cp = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    return cp.returncode == 0 and str(pid) in cp.stdout


def stop_worker() -> None:
    if not PID_FILE.is_file():
        return
    try:
        pid = int(PID_FILE.read_text(encoding="ascii").strip())
    except ValueError:
        PID_FILE.unlink(missing_ok=True)
        return
    if pid and process_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and process_alive(pid):
            time.sleep(0.5)
        if process_alive(pid):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False)
    PID_FILE.unlink(missing_ok=True)


def read_dsn(path: Path) -> str:
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.startswith("DATABASE_URL="):
            dsn = raw.split("=", 1)[1].strip()
            parsed = urlparse(dsn)
            if not (parsed.hostname or "").endswith(".neon.tech"):
                raise SystemExit("DATABASE_URL is not Neon")
            if (parsed.path or "").lstrip("/") != EXPECTED_DB:
                raise SystemExit("DATABASE_URL database mismatch")
            return dsn
    raise SystemExit("DATABASE_URL missing")


def replace_database_url(path: Path, dsn: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    replaced = False
    for line in lines:
        if line.startswith("DATABASE_URL="):
            if not replaced:
                out.append("DATABASE_URL=" + dsn)
                replaced = True
            continue
        out.append(line)
    if not replaced:
        out.append("DATABASE_URL=" + dsn)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def deploy() -> None:
    cp = subprocess.run(
        [os.environ.get("PYTHON", "python"), "-m", "scripts.deploy_pc24x7_noteri_ha_worker_dev"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if cp.returncode != 0:
        raise RuntimeError((cp.stderr or cp.stdout)[-800:])


def main() -> int:
    if not RUNTIME_ENV.is_file() or not SECRET_ENV.is_file():
        raise SystemExit("runtime or Neon secret env missing")
    dsn = read_dsn(SECRET_ENV)
    if not BACKUP_ENV.exists():
        shutil.copy2(RUNTIME_ENV, BACKUP_ENV)
    try:
        replace_database_url(RUNTIME_ENV, dsn)
        stop_worker()
        deploy()
    except Exception:
        shutil.copy2(BACKUP_ENV, RUNTIME_ENV)
        stop_worker()
        deploy()
        raise

    if not PID_FILE.is_file():
        raise SystemExit("worker pid missing after cutover")
    pid = int(PID_FILE.read_text(encoding="ascii").strip())
    if not process_alive(pid):
        raise SystemExit("worker not running after cutover")
    print(json.dumps({
        "status": "ok",
        "database": EXPECTED_DB,
        "node_id": "Noteri",
        "worker_running": True,
        "local_bridge_no_longer_required": True,
        "rollback_env_preserved": True,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

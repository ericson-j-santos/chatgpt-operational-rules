from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
BASE = Path(os.environ["LOCALAPPDATA"]) / "ReqSys" / "TodoGlobal24x7"
RUNTIME_ENV = BASE / "runtime.env"
SECRET_ENV = BASE / "neon-ha.env"
BACKUP_ENV = BASE / "runtime.env.pre-neon-ha"
COMPOSE = ROOT / "docker-compose.pc24x7.yml"
EXPECTED_DB = "todo_global_bus_dev_ha"


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


def compose_recreate() -> None:
    subprocess.run([
        "docker", "compose", "--env-file", str(RUNTIME_ENV),
        "-f", str(COMPOSE), "up", "-d", "--no-deps", "--force-recreate",
        "gateway", "worker",
    ], cwd=ROOT, check=True)


def wait_ready(timeout: int = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen("http://127.0.0.1:8094/readyz", timeout=3) as response:
                if 200 <= response.status < 300:
                    return
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError("gateway readiness timeout")


def assert_container_dsn(container: str) -> None:
    code = (
        "import os,urllib.parse;"
        "u=urllib.parse.urlparse(os.environ['DATABASE_URL']);"
        "assert (u.hostname or '').endswith('.neon.tech');"
        "assert u.path.lstrip('/')=='todo_global_bus_dev_ha'"
    )
    subprocess.run(["docker", "exec", container, "python", "-c", code], check=True)


def main() -> int:
    if not RUNTIME_ENV.is_file() or not SECRET_ENV.is_file():
        raise SystemExit("runtime or Neon secret env missing")
    dsn = read_dsn(SECRET_ENV)
    if not BACKUP_ENV.exists():
        shutil.copy2(RUNTIME_ENV, BACKUP_ENV)
    try:
        replace_database_url(RUNTIME_ENV, dsn)
        compose_recreate()
        wait_ready()
        assert_container_dsn("todo-global-24x7-gateway-1")
        assert_container_dsn("todo-global-24x7-worker-1")
    except Exception:
        shutil.copy2(BACKUP_ENV, RUNTIME_ENV)
        compose_recreate()
        raise
    print(json.dumps({
        "status": "ok",
        "database": EXPECTED_DB,
        "gateway_ready": True,
        "gateway_external_db": True,
        "worker_external_db": True,
        "local_db_preserved_for_rollback": True,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

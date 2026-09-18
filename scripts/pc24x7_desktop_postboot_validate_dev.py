from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

BASE = Path(os.environ["LOCALAPPDATA"]) / "ReqSys" / "TodoGlobal24x7"
EVIDENCE_DIR = BASE / "evidence"
EVIDENCE_FILE = EVIDENCE_DIR / "postboot-last.json"
ACTIVE_REPO = BASE / "active-repo-root.txt"
RUNTIME_ENV = BASE / "runtime.env"
DOCKER = Path.home() / "AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe"
ROLLBACK_CONTAINERS = ("todo-global-24x7-db-1", "todo-global-24x7-db_bridge-1")
RESULT: dict[str, object] = {
    "generated_at": datetime.now(UTC).isoformat(),
    "host_role": "desktop-primary",
    "contract": "pc24x7-neon-postboot-v1",
}


def run(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(DOCKER), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def load_runtime_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in RUNTIME_ENV.read_text(encoding="utf-8").splitlines():
        if not raw or raw.lstrip().startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def canonical_database(values: dict[str, str]) -> bool:
    dsn = values.get("DATABASE_URL", "")
    parsed = urlparse(dsn)
    return (
        bool(parsed.hostname)
        and str(parsed.hostname).endswith(".neon.tech")
        and (parsed.path or "").lstrip("/") == "todo_global_bus_dev_ha"
    )


def probe(path: str) -> dict[str, object]:
    try:
        with urlopen(f"http://127.0.0.1:8094{path}", timeout=5) as response:
            return {"ok": 200 <= response.status < 300, "status_code": response.status}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def inspect_running(name: str) -> bool:
    cp = run(["inspect", "-f", "{{.State.Running}}", name], timeout=15)
    return cp.returncode == 0 and cp.stdout.strip().lower() == "true"


def disable_rollback_autostart(name: str) -> dict[str, object]:
    exists = run(["inspect", name], timeout=15).returncode == 0
    if not exists:
        return {"exists": False, "running": False, "restart_policy_disabled": True}
    if inspect_running(name):
        run(["stop", name], timeout=45)
    update = run(["update", "--restart=no", name], timeout=30)
    return {
        "exists": True,
        "running": inspect_running(name),
        "restart_policy_disabled": update.returncode == 0,
    }


def verify_gateway_database() -> dict[str, object]:
    code = (
        "import os,urllib.parse,psycopg,json;"
        "u=urllib.parse.urlparse(os.environ['DATABASE_URL']);"
        "c=psycopg.connect(os.environ['DATABASE_URL']);"
        "r=c.execute('SELECT 1').fetchone();"
        "print(json.dumps({"
        "'host_neon': bool(u.hostname and u.hostname.endswith('.neon.tech')),"
        "'database': u.path.lstrip('/'),"
        "'select1': int(r[0]) if r else None"
        "}));"
        "c.close()"
    )
    cp = run(["exec", "todo-global-24x7-gateway-1", "python", "-c", code], timeout=30)
    if cp.returncode != 0:
        return {"ok": False, "error": (cp.stderr or cp.stdout)[-500:]}
    try:
        payload = json.loads(cp.stdout.strip().splitlines()[-1])
    except Exception:
        return {"ok": False, "error": "invalid gateway database proof"}
    payload["ok"] = (
        payload.get("host_neon") is True
        and payload.get("database") == "todo_global_bus_dev_ha"
        and payload.get("select1") == 1
    )
    return payload


def main() -> int:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    if not ACTIVE_REPO.is_file():
        RESULT.update({"ready": False, "status": "active_repo_pointer_missing"})
        EVIDENCE_FILE.write_text(json.dumps(RESULT, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(RESULT))
        return 2

    repo_root = Path(ACTIVE_REPO.read_text(encoding="utf-8").strip())
    compose = repo_root / "docker-compose.pc24x7.yml"
    if not repo_root.is_dir() or not compose.is_file():
        RESULT.update({"ready": False, "status": "active_repo_invalid"})
        EVIDENCE_FILE.write_text(json.dumps(RESULT, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(RESULT))
        return 2

    values = load_runtime_env()
    RESULT["canonical_database"] = canonical_database(values)
    if not RESULT["canonical_database"]:
        RESULT.update({"ready": False, "status": "noncanonical_database"})
        EVIDENCE_FILE.write_text(json.dumps(RESULT, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(RESULT))
        return 3

    start = run(["desktop", "start"], timeout=60)
    RESULT["docker_start_rc"] = start.returncode
    docker_ready = False
    for _ in range(90):
        info = run(["info", "--format", "{{.ServerVersion}}"], timeout=10)
        if info.returncode == 0 and info.stdout.strip():
            RESULT["docker_engine"] = info.stdout.strip()
            docker_ready = True
            break
        time.sleep(2)
    RESULT["docker_ready"] = docker_ready
    if not docker_ready:
        RESULT.update({"ready": False, "status": "docker_not_ready"})
        EVIDENCE_FILE.write_text(json.dumps(RESULT, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(RESULT))
        return 4

    rollback = {name: disable_rollback_autostart(name) for name in ROLLBACK_CONTAINERS}
    RESULT["rollback_containers"] = rollback

    compose_up = run([
        "compose",
        "--env-file", str(RUNTIME_ENV),
        "-f", str(compose),
        "up", "-d", "--build",
        "gateway", "worker", "ingest",
    ], timeout=300)
    RESULT["compose_rc"] = compose_up.returncode
    if compose_up.returncode != 0:
        RESULT["compose_error_tail"] = (compose_up.stderr or compose_up.stdout)[-1000:]

    ready = False
    health = readiness = {}
    for _ in range(60):
        health = probe("/healthz")
        readiness = probe("/readyz")
        if health.get("ok") and readiness.get("ok"):
            ready = True
            break
        time.sleep(2)

    db_proof = verify_gateway_database() if ready else {"ok": False, "error": "runtime_not_ready"}
    RESULT["healthz"] = health
    RESULT["readyz"] = readiness
    RESULT["runtime_ready"] = ready
    RESULT["database_proof"] = db_proof
    RESULT["active_repo_root"] = str(repo_root)
    RESULT["rollback_stopped"] = all(
        not bool(item.get("running")) and bool(item.get("restart_policy_disabled"))
        for item in rollback.values()
    )
    RESULT["ready"] = bool(
        docker_ready
        and compose_up.returncode == 0
        and ready
        and db_proof.get("ok")
        and RESULT["rollback_stopped"]
    )
    RESULT["status"] = "ready" if RESULT["ready"] else "validation_failed"
    EVIDENCE_FILE.write_text(
        json.dumps(RESULT, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(RESULT, ensure_ascii=True))
    return 0 if RESULT["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen

DB_NAME = "todo_global_bus_dev"
EVIDENCE_DIR = Path("/var/lib/todo-global/evidence")
BACKUP_DIR = Path("/var/lib/todo-global/backups")


def require_root() -> None:
    if os.name != "posix" or os.geteuid() != 0:
        raise RuntimeError("resilience proof must run as root")


def run(args: list[str], *, input_bytes: bytes | None = None, timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, input=input_bytes, capture_output=True, timeout=timeout, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(f"command failed rc={result.returncode}: {args[0]}")
    return result


def psql_scalar(database: str, sql: str) -> str:
    result = run(["runuser", "-u", "postgres", "--", "psql", "-d", database, "-At", "-c", sql], timeout=30)
    return result.stdout.decode("utf-8", errors="replace").strip()


def load_e2e() -> dict:
    path = EVIDENCE_DIR / "e2e-last.json"
    if not path.is_file():
        raise RuntimeError("E2E evidence missing")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("ready"):
        raise RuntimeError("latest E2E evidence is not ready")
    return data


def verify_rows(database: str, evidence: dict) -> dict[str, bool]:
    event_id = evidence["event_id"]
    key = evidence["idempotency_key"]
    request_id = evidence["request_id"]
    event_count = psql_scalar(database, f"SELECT count(*) FROM todo_bus.queue_events WHERE event_id = '{event_id}'")
    cont_count = psql_scalar(database, f"SELECT count(*) FROM todo_bus.continuation_requests WHERE request_id = '{request_id}' AND idempotency_key = '{key}'")
    return {"event_present": event_count == "1", "continuation_present": cont_count == "1"}


def smoke(wait_seconds: int = 90) -> bool:
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        try:
            with urlopen("http://127.0.0.1:8000/readyz", timeout=3) as response:
                if response.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def restart_proof(evidence: dict) -> dict:
    before = verify_rows(DB_NAME, evidence)
    run(["systemctl", "restart", "postgresql"])
    run(["systemctl", "restart", "todo-global-gateway.service"])
    ready = smoke()
    after = verify_rows(DB_NAME, evidence) if ready else {"event_present": False, "continuation_present": False}
    return {
        "restart_ready": ready,
        "before": before,
        "after": after,
        "persistent": all(before.values()) and all(after.values()),
    }


def backup_restore_proof(evidence: dict) -> dict:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = BACKUP_DIR / f"todo-global-{stamp}.dump"
    dump = run(["runuser", "-u", "postgres", "--", "pg_dump", "-d", DB_NAME, "-Fc"], timeout=120)
    if not dump.stdout:
        raise RuntimeError("pg_dump produced no data")
    backup_path.write_bytes(dump.stdout)
    digest = hashlib.sha256(dump.stdout).hexdigest()

    restore_db = f"todo_restore_{uuid.uuid4().hex[:10]}"
    run(["runuser", "-u", "postgres", "--", "createdb", restore_db], timeout=30)
    try:
        run([
            "runuser", "-u", "postgres", "--", "pg_restore",
            "-d", restore_db, "--no-owner", "--no-privileges",
        ], input_bytes=dump.stdout, timeout=120)
        verified = verify_rows(restore_db, evidence)
    finally:
        run(["runuser", "-u", "postgres", "--", "dropdb", "--if-exists", restore_db], timeout=30, check=False)

    return {
        "backup_file": str(backup_path),
        "backup_size": len(dump.stdout),
        "backup_sha256": digest,
        "restore_event_present": verified["event_present"],
        "restore_continuation_present": verified["continuation_present"],
        "restore_verified": all(verified.values()),
    }


def main() -> int:
    require_root()
    evidence = load_e2e()
    restart = restart_proof(evidence)
    backup_restore = backup_restore_proof(evidence)
    ready = bool(restart["persistent"] and backup_restore["restore_verified"])
    result = {
        "contract": "todo-global-debian-resilience",
        "generated_at": datetime.now(UTC).isoformat(),
        "host_role": "debian-vm-primary",
        "restart": restart,
        "backup_restore": backup_restore,
        "ready": ready,
    }
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / "resilience-last.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())

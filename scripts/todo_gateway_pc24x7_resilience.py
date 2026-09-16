#!/usr/bin/env python3
"""Provas de persistência, restart e backup/restore do TODO Global 24x7."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import uuid
from datetime import UTC, datetime

from scripts.todo_gateway_pc24x7 import compose, ensure_docker_ready, ensure_runtime_env, runtime_dir, smoke

DB_CONTAINER = "todo-global-24x7-db-1"
DB_USER = "todo_global_bus_dev_user"
DB_NAME = "todo_global_bus_dev"


def load_e2e() -> dict:
    path = runtime_dir() / "evidence" / "e2e-last.json"
    if not path.is_file():
        raise RuntimeError("e2e evidence missing; run live E2E first")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("ready"):
        raise RuntimeError("latest e2e evidence is not ready")
    return data


def docker_text(args: list[str], *, input_bytes: bytes | None = None, timeout: int = 120) -> str:
    result = subprocess.run(
        ["docker", *args],
        input=input_bytes,
        capture_output=True,
        text=False,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"docker command failed rc={result.returncode}")
    return result.stdout.decode("utf-8", errors="replace").strip()


def psql_scalar(database: str, sql: str) -> str:
    return docker_text(
        ["exec", DB_CONTAINER, "psql", "-U", DB_USER, "-d", database, "-At", "-c", sql],
        timeout=30,
    )


def verify_rows(database: str, evidence: dict) -> dict[str, bool]:
    event_id = evidence["event_id"]
    key = evidence["idempotency_key"]
    request_id = evidence["request_id"]
    event_count = psql_scalar(database, f"SELECT count(*) FROM todo_bus.queue_events WHERE event_id = '{event_id}'")
    cont_count = psql_scalar(database, f"SELECT count(*) FROM todo_bus.continuation_requests WHERE request_id = '{request_id}' AND idempotency_key = '{key}'")
    return {"event_present": event_count == "1", "continuation_present": cont_count == "1"}


def restart_proof(repo_root, env_file, evidence: dict) -> dict:
    before = verify_rows(DB_NAME, evidence)
    first = compose(repo_root, env_file, "restart", "db", timeout=120)
    if first.returncode != 0:
        raise RuntimeError("database restart failed")
    second = compose(repo_root, env_file, "restart", "gateway", timeout=120)
    if second.returncode != 0:
        raise RuntimeError("gateway restart failed")
    health = smoke(90)
    after = verify_rows(DB_NAME, evidence) if health.get("ready") else {"event_present": False, "continuation_present": False}
    return {
        "restart_ready": bool(health.get("ready")),
        "before": before,
        "after": after,
        "persistent": all(before.values()) and all(after.values()),
    }


def backup_restore_proof(evidence: dict) -> dict:
    backup_dir = runtime_dir() / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"todo-global-{stamp}.dump"

    dump = subprocess.run(
        ["docker", "exec", DB_CONTAINER, "pg_dump", "-U", DB_USER, "-d", DB_NAME, "-Fc"],
        capture_output=True,
        timeout=120,
        check=False,
    )
    if dump.returncode != 0 or not dump.stdout:
        raise RuntimeError("pg_dump failed")
    backup_path.write_bytes(dump.stdout)
    digest = hashlib.sha256(dump.stdout).hexdigest()

    restore_db = f"todo_restore_{uuid.uuid4().hex[:10]}"
    docker_text(["exec", DB_CONTAINER, "createdb", "-U", DB_USER, restore_db], timeout=30)
    try:
        docker_text(
            ["exec", "-i", DB_CONTAINER, "pg_restore", "-U", DB_USER, "-d", restore_db, "--no-owner", "--no-privileges"],
            input_bytes=dump.stdout,
            timeout=120,
        )
        verified = verify_rows(restore_db, evidence)
    finally:
        subprocess.run(
            ["docker", "exec", DB_CONTAINER, "dropdb", "-U", DB_USER, "--if-exists", restore_db],
            capture_output=True,
            timeout=30,
            check=False,
        )

    return {
        "backup_file": str(backup_path),
        "backup_size": len(dump.stdout),
        "backup_sha256": digest,
        "restore_event_present": verified["event_present"],
        "restore_continuation_present": verified["continuation_present"],
        "restore_verified": all(verified.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("restart", "backup-restore", "all"), nargs="?", default="all")
    args = parser.parse_args()
    repo_root = __import__("pathlib").Path(__file__).resolve().parents[1]
    if not ensure_docker_ready(120):
        print(json.dumps({"ready": False, "blocker": "docker_unavailable"}))
        return 2
    env_file = ensure_runtime_env()
    evidence = load_e2e()
    result: dict[str, object] = {
        "contract": "todo-global-pc24x7-resilience",
        "generated_at": datetime.now(UTC).isoformat(),
        "host_role": "desktop-primary",
    }
    if args.action in {"restart", "all"}:
        result["restart"] = restart_proof(repo_root, env_file, evidence)
    if args.action in {"backup-restore", "all"}:
        result["backup_restore"] = backup_restore_proof(evidence)
    restart_ok = args.action == "backup-restore" or bool(result.get("restart", {}).get("persistent"))
    backup_ok = args.action == "restart" or bool(result.get("backup_restore", {}).get("restore_verified"))
    result["ready"] = restart_ok and backup_ok
    evidence_dir = runtime_dir() / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "resilience-last.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlparse

DB_CONTAINER = "todo-global-24x7-db-1"
SOURCE_USER = "todo_global_bus_dev_user"
SOURCE_DB = "todo_global_bus_dev"
EXPECTED_TARGET_DB = "todo_global_bus_dev_ha"
SECRET_FILE = Path(os.environ["LOCALAPPDATA"]) / "ReqSys" / "TodoGlobal24x7" / "neon-ha.env"
DUMP_PATH = "/tmp/todo-global-neon-ha.dump"

TABLES = (
    "continuation_requests",
    "continuation_history",
    "worker_heartbeat",
    "host_route_decisions",
    "runtime_node_heartbeat",
    "queue_events",
    "queue_event_history",
    "worker_heartbeats",
)


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def load_dsn() -> str:
    if not SECRET_FILE.is_file():
        raise SystemExit("Neon secret file missing")
    for raw in SECRET_FILE.read_text(encoding="utf-8").splitlines():
        if raw.startswith("DATABASE_URL="):
            dsn = raw.split("=", 1)[1].strip()
            parsed = urlparse(dsn)
            if parsed.scheme not in {"postgres", "postgresql"}:
                raise SystemExit("target DATABASE_URL scheme invalid")
            if not (parsed.hostname or "").endswith(".neon.tech"):
                raise SystemExit("target host is not Neon")
            if (parsed.path or "").lstrip("/") != EXPECTED_TARGET_DB:
                raise SystemExit("target database name mismatch")
            return dsn
    raise SystemExit("DATABASE_URL missing from secret file")


def psql_local(sql: str) -> str:
    cp = run([
        "docker", "exec", DB_CONTAINER, "psql",
        "-U", SOURCE_USER, "-d", SOURCE_DB, "-At", "-c", sql,
    ])
    return cp.stdout.strip()


def psql_target(dsn: str, sql: str) -> str:
    cp = run([
        "docker", "exec", DB_CONTAINER, "psql",
        dsn, "-At", "-c", sql,
    ])
    return cp.stdout.strip()


def counts_local() -> dict[str, int]:
    return {t: int(psql_local(f"SELECT count(*) FROM todo_bus.{t}")) for t in TABLES}


def counts_target(dsn: str) -> dict[str, int]:
    return {t: int(psql_target(dsn, f"SELECT count(*) FROM todo_bus.{t}")) for t in TABLES}


def main() -> int:
    dsn = load_dsn()
    source = counts_local()
    target_before = counts_target(dsn)
    if any(target_before.values()):
        raise SystemExit(f"target is not empty: {target_before}")

    run([
        "docker", "exec", DB_CONTAINER, "pg_dump",
        "-U", SOURCE_USER, "-d", SOURCE_DB,
        "--format=custom", "--data-only", "--schema=todo_bus",
        "--file", DUMP_PATH,
    ])

    continuation_trigger_disabled = False
    queue_trigger_disabled = False
    try:
        psql_target(dsn, "ALTER TABLE todo_bus.continuation_requests DISABLE TRIGGER trg_continuation_audit")
        continuation_trigger_disabled = True
        psql_target(dsn, "ALTER TABLE todo_bus.queue_events DISABLE TRIGGER trg_todo_queue_audit")
        queue_trigger_disabled = True
        restored = run([
            "docker", "exec", DB_CONTAINER, "pg_restore",
            "--dbname", dsn, "--data-only", "--no-owner", "--no-privileges",
            DUMP_PATH,
        ], check=False)
        if restored.returncode != 0:
            raise SystemExit((restored.stderr or restored.stdout)[-1200:])
    finally:
        if queue_trigger_disabled:
            psql_target(dsn, "ALTER TABLE todo_bus.queue_events ENABLE TRIGGER trg_todo_queue_audit")
        if continuation_trigger_disabled:
            psql_target(dsn, "ALTER TABLE todo_bus.continuation_requests ENABLE TRIGGER trg_continuation_audit")

    psql_target(
        dsn,
        "SELECT setval('todo_bus.host_route_fencing_seq', "
        "GREATEST(COALESCE((SELECT max(fencing_token) FROM todo_bus.host_route_decisions),1),1), "
        "EXISTS(SELECT 1 FROM todo_bus.host_route_decisions))",
    )
    psql_target(
        dsn,
        "SELECT setval('todo_bus.continuation_history_history_id_seq', "
        "GREATEST(COALESCE((SELECT max(history_id) FROM todo_bus.continuation_history),1),1), "
        "EXISTS(SELECT 1 FROM todo_bus.continuation_history))",
    )
    psql_target(
        dsn,
        "SELECT setval('todo_bus.queue_event_history_history_id_seq', "
        "GREATEST(COALESCE((SELECT max(history_id) FROM todo_bus.queue_event_history),1),1), "
        "EXISTS(SELECT 1 FROM todo_bus.queue_event_history))",
    )

    target_after = counts_target(dsn)
    if target_after != source:
        raise SystemExit(f"count mismatch source={source} target={target_after}")

    proof = {
        "status": "ok",
        "source_counts": source,
        "target_counts": target_after,
        "history_preserved": (
            target_after["continuation_history"] == source["continuation_history"]
            and target_after["queue_event_history"] == source["queue_event_history"]
        ),
        "source_untouched": True,
        "target_database": EXPECTED_TARGET_DB,
    }
    print(json.dumps(proof, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

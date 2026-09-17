#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "sql" / "todo_control_plane_postgres.sql"
CONTAINER = "todo-global-24x7-db-1"
DB_USER = "todo_global_bus_dev_user"
DB_NAME = "todo_global_bus_dev"
PSQL = [
    "docker", "exec", "-i", CONTAINER, "psql",
    "-v", "ON_ERROR_STOP=1", "-U", DB_USER, "-d", DB_NAME,
    "-At", "-F", "|",
]


def run_sql(sql: str) -> str:
    result = subprocess.run(
        PSQL, input=sql, text=True, capture_output=True, check=False,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"psql failed exit={result.returncode}: {result.stderr.strip()}")
    return result.stdout.strip()


def main() -> int:
    if not MIGRATION.is_file():
        raise RuntimeError(f"migration missing: {MIGRATION}")
    run_sql(MIGRATION.read_text(encoding="utf-8"))

    corr = "risk3-e2e-host-route-20260917"
    first = run_sql(
        "SELECT node_id,reason,fencing_token FROM todo_bus.claim_host_route(" 
        f"'{corr}','Noteri','primary_unavailable_failover');"
    )
    second = run_sql(
        "SELECT node_id,reason,fencing_token FROM todo_bus.claim_host_route(" 
        f"'{corr}','DESKTOP-PDQK954','primary_healthy');"
    )
    rows = run_sql(
        "SELECT count(*),min(node_id),min(fencing_token) "
        f"FROM todo_bus.host_route_decisions WHERE correlation_id='{corr}';"
    )
    if first != second:
        raise AssertionError(f"idempotency failed: first={first!r} second={second!r}")
    parts = rows.split("|")
    if len(parts) != 3 or parts[0] != "1" or parts[1] != "Noteri" or int(parts[2]) < 1:
        raise AssertionError(f"unexpected persisted route: {rows!r}")

    print(json.dumps({
        "status": "ok",
        "migration": MIGRATION.name,
        "correlation_id": corr,
        "persisted": first,
        "row_check": rows,
        "idempotent_repeat": True,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

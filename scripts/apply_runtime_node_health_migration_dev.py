from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SQL_FILE = ROOT / "sql" / "pc24x7_runtime_node_health.sql"
DB_CONTAINER = "todo-global-24x7-db-1"
DB_USER = "todo_global_bus_dev_user"
DB_NAME = "todo_global_bus_dev"


def main() -> int:
    sql = SQL_FILE.read_text(encoding="utf-8")
    applied = subprocess.run(
        ["docker", "exec", "-i", DB_CONTAINER, "psql", "-v", "ON_ERROR_STOP=1",
         "-U", DB_USER, "-d", DB_NAME],
        input=sql,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if applied.returncode != 0:
        raise SystemExit((applied.stderr or applied.stdout)[-800:])
    verify = subprocess.run(
        ["docker", "exec", DB_CONTAINER, "psql", "-U", DB_USER, "-d", DB_NAME, "-At", "-c",
         "SELECT to_regclass('todo_bus.runtime_node_heartbeat') IS NOT NULL,"
         "to_regprocedure('todo_bus.touch_runtime_node_heartbeat(text,text,jsonb,integer)') IS NOT NULL,"
         "to_regprocedure('todo_bus.get_runtime_node_health(text,text,integer,integer)') IS NOT NULL"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=True,
    )
    if verify.stdout.strip() != "t|t|t":
        raise SystemExit("runtime node health migration verification failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

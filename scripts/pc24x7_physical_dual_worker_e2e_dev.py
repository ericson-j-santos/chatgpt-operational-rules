from __future__ import annotations

import hashlib
import json
import subprocess
import time
import uuid

DB_CONTAINER = "todo-global-24x7-db-1"
DB_USER = "todo_global_bus_dev_user"
DB_NAME = "todo_global_bus_dev"


def psql(sql: str) -> str:
    result = subprocess.run(
        ["docker", "exec", DB_CONTAINER, "psql", "-U", DB_USER, "-d", DB_NAME, "-At", "-F", "|", "-c", sql],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[-500:])
    return result.stdout.strip()


def main() -> int:
    suffix = uuid.uuid4().hex[:12]
    corr = f"physical-dual-worker-{suffix}"
    req = f"req-{suffix}"
    idem = hashlib.sha256(corr.encode("utf-8")).hexdigest()
    basis = f"evt-{suffix}"
    payload = json.dumps({
        "automation_action": "ai_control_plane.validate_idempotent_continuation.v1",
        "next_action": "validar continuidade idempotente",
        "external_id": f"desktop-24x7-e2e-{suffix}",
    }, separators=(",", ":"))
    route = psql(
        "SELECT node_id,reason,fencing_token FROM todo_bus.claim_host_route(" 
        f"'{corr}','Noteri','physical_failover_e2e');"
    )
    psql(
        "INSERT INTO todo_bus.continuation_requests(" 
        "request_id,idempotency_key,basis_event_id,correlation_id,project,todo_payload" 
        f") VALUES ('{req}','{idem}','{basis}','{corr}','AI Control Plane','{payload}'::jsonb);"
    )
    state = ""
    attempts = -1
    for _ in range(40):
        row = psql(f"SELECT state,attempts FROM todo_bus.continuation_requests WHERE request_id='{req}';")
        if row:
            state, attempts_raw = row.split("|", 1)
            attempts = int(attempts_raw)
            if state in {"COMPLETED", "DLQ", "HUMAN_GATE"}:
                break
        time.sleep(0.5)
    history = psql(
        "SELECT string_agg(to_state,',' ORDER BY history_id) "
        f"FROM todo_bus.continuation_history WHERE request_id='{req}';"
    )
    if state != "COMPLETED":
        raise AssertionError(f"continuation final state={state!r} attempts={attempts}")
    if attempts != 1:
        raise AssertionError(f"expected one execution attempt, got {attempts}")
    if history != "PENDING,PROCESSING,COMPLETED":
        raise AssertionError(f"unexpected history: {history!r}")
    route_parts = route.split("|")
    if len(route_parts) != 3 or route_parts[0] != "Noteri":
        raise AssertionError(f"unexpected route: {route!r}")
    print(json.dumps({
        "status": "ok",
        "request_id": req,
        "correlation_id": corr,
        "route_owner": route_parts[0],
        "route_reason": route_parts[1],
        "fencing_token": int(route_parts[2]),
        "final_state": state,
        "attempts": attempts,
        "history": history,
        "single_owner_execution": True,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

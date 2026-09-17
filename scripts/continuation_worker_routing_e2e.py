#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os

from scripts.continuation_worker import PostgresContinuationQueue, process_batch
from scripts.host_router import HostRouter, NodeHealth

DESKTOP = "DESKTOP-PDQK954"
NOTERI = "Noteri"
REQUEST_ID = "worker-route-e2e-20260917"
CORRELATION_ID = "worker-route-corr-e2e-20260917"


class TargetQueue(PostgresContinuationQueue):
    def reserve_continuations_for_node(self, node_id: str, limit: int, lease_seconds: int):
        sql = """
        WITH eligible AS (
          SELECT c.request_id
          FROM todo_bus.continuation_requests c
          LEFT JOIN todo_bus.host_route_decisions d ON d.correlation_id=c.correlation_id
          WHERE c.request_id=%s
            AND ((c.state='PENDING' AND c.available_at<=clock_timestamp())
              OR (c.state='PROCESSING' AND c.lease_until<=clock_timestamp()))
            AND (d.node_id IS NULL OR d.node_id=%s)
          FOR UPDATE OF c SKIP LOCKED
        )
        UPDATE todo_bus.continuation_requests c
        SET state='PROCESSING', attempts=c.attempts+1,
            lease_until=clock_timestamp()+make_interval(secs => %s),
            updated_at=clock_timestamp()
        FROM eligible e WHERE c.request_id=e.request_id RETURNING c.*
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (REQUEST_ID, node_id, lease_seconds))
            rows = cur.fetchall()
        return self._items(rows)


def state(queue: TargetQueue):
    with queue._connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT state,attempts FROM todo_bus.continuation_requests WHERE request_id=%s", (REQUEST_ID,))
        row = cur.fetchone()
        cur.execute("SELECT node_id,fencing_token FROM todo_bus.host_route_decisions WHERE correlation_id=%s", (CORRELATION_ID,))
        route = cur.fetchone()
    return row, route


def cleanup(queue: TargetQueue):
    with queue._connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM todo_bus.continuation_history WHERE request_id=%s", (REQUEST_ID,))
        cur.execute("DELETE FROM todo_bus.continuation_requests WHERE request_id=%s", (REQUEST_ID,))
        cur.execute("DELETE FROM todo_bus.host_route_decisions WHERE correlation_id=%s", (CORRELATION_ID,))


def main() -> int:
    queue = TargetQueue(os.environ.get("DATABASE_URL", ""))
    cleanup(queue)
    payload = {"automation_action":"ai_control_plane.validate_idempotent_continuation.v1",
               "next_action":"validar continuidade idempotente",
               "external_id":"desktop-24x7-e2e-routing-owner"}
    with queue._connect() as conn, conn.cursor() as cur:
        cur.execute("""
          INSERT INTO todo_bus.continuation_requests
          (request_id,idempotency_key,basis_event_id,correlation_id,project,todo_payload,state,attempts,available_at,created_at,updated_at)
          VALUES (%s,%s,%s,%s,'AI Control Plane',%s::jsonb,'PENDING',0,clock_timestamp(),clock_timestamp(),clock_timestamp())
        """, (REQUEST_ID, hashlib.sha256(REQUEST_ID.encode()).hexdigest(), "basis-worker-route-e2e",
              CORRELATION_ID, json.dumps(payload)))
    nodes = [NodeHealth(DESKTOP, False), NodeHealth(NOTERI, True)]
    desktop = process_batch(queue, limit=1, router=HostRouter(DESKTOP, NOTERI), node_id=DESKTOP, nodes=nodes)
    after_desktop, route = state(queue)
    noteri = process_batch(queue, limit=1, router=HostRouter(DESKTOP, NOTERI), node_id=NOTERI, nodes=nodes)
    after_noteri, route2 = state(queue)
    ok = (desktop["routed_elsewhere"] == 1 and desktop["completed"] == 0
          and after_desktop == ("PENDING", 0) and route and route[0] == NOTERI
          and noteri["completed"] == 1 and after_noteri == ("COMPLETED", 1)
          and route == route2)
    result = {"status":"ok" if ok else "failed", "desktop":desktop, "noteri":noteri,
              "after_desktop":after_desktop, "after_noteri":after_noteri,
              "route_owner":route[0] if route else None, "fencing_token":int(route[1]) if route else None}
    cleanup(queue)
    print(json.dumps(result, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

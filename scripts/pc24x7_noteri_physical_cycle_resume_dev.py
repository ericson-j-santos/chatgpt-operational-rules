from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import uuid
import hashlib
from pathlib import Path

import psycopg

DESKTOP = "DESKTOP-PDQK954"
NOTERI = "Noteri"
NOTERI_SERVICE_WORKER = "continuation-worker-Noteri-ha-service"
DESKTOP_WORKER_CONTAINER = "todo-global-24x7-worker-1"
TTL_SECONDS = 20
STABILITY_SECONDS = 15
BASE = Path(os.environ["LOCALAPPDATA"]) / "ReqSys" / "TodoGlobal24x7"
ENV_FILE = BASE / "runtime.env"
EVIDENCE_DIR = BASE / "evidence"
EVIDENCE_FILE = EVIDENCE_DIR / "noteri-physical-cycle-e2e-last.json"


def load_dsn() -> str:
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if raw.startswith("DATABASE_URL="):
            dsn = raw.split("=", 1)[1].strip()
            if ".neon.tech/" not in dsn:
                raise SystemExit("DATABASE_URL is not Neon")
            return dsn
    raise SystemExit("DATABASE_URL missing")


def save(stage: str, **extra: object) -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "stage": stage,
        "observed_at_epoch": time.time(),
        "observer_host": socket.gethostname(),
        **extra,
    }
    EVIDENCE_FILE.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def query(conn: psycopg.Connection, sql: str, params: tuple[object, ...] = ()) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def health(conn: psycopg.Connection) -> dict[str, dict[str, int | bool]]:
    rows = query(
        conn,
        "SELECT node_id,healthy,heartbeat_age_seconds,healthy_age_seconds "
        "FROM todo_bus.get_runtime_node_health(%s,%s,%s,%s)",
        (DESKTOP, NOTERI, TTL_SECONDS, STABILITY_SECONDS),
    )
    return {
        str(node): {
            "healthy": bool(ok),
            "heartbeat_age_seconds": int(hb_age),
            "healthy_age_seconds": int(healthy_age),
        }
        for node, ok, hb_age, healthy_age in rows
    }


def wait_node(
    conn: psycopg.Connection,
    node: str,
    expected: bool,
    timeout_seconds: int,
    stage: str,
) -> dict[str, dict[str, int | bool]]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, dict[str, int | bool]] = {}
    while time.monotonic() < deadline:
        last = health(conn)
        save(stage, health=last)
        if bool(last.get(node, {}).get("healthy")) is expected:
            return last
        time.sleep(1)
    raise AssertionError(f"health timeout node={node} expected={expected} last={last}")


def service_heartbeat_age(conn: psycopg.Connection) -> int:
    rows = query(
        conn,
        "SELECT round(extract(epoch from clock_timestamp()-heartbeat_at)) "
        "FROM todo_bus.worker_heartbeat WHERE worker_id=%s",
        (NOTERI_SERVICE_WORKER,),
    )
    return -1 if not rows else int(rows[0][0])


def wait_service_heartbeat(conn: psycopg.Connection, timeout_seconds: int = 300) -> int:
    deadline = time.monotonic() + timeout_seconds
    last = -1
    while time.monotonic() < deadline:
        last = service_heartbeat_age(conn)
        save("waiting_noteri_service_heartbeat", service_heartbeat_age_seconds=last, health=health(conn))
        if 0 <= last <= 15:
            return last
        time.sleep(2)
    raise AssertionError(f"Noteri service heartbeat timeout age={last}")


def latest_desktop_survival(conn: psycopg.Connection) -> dict[str, object]:
    rows = query(
        conn,
        "SELECT c.request_id,c.correlation_id,c.state,c.attempts,"
        "coalesce(d.node_id,''),coalesce(d.reason,''),coalesce(d.fencing_token,0) "
        "FROM todo_bus.continuation_requests c "
        "LEFT JOIN todo_bus.host_route_decisions d ON d.correlation_id=c.correlation_id "
        "WHERE c.correlation_id LIKE 'noteri-physical-cycle-desktop-survival-%' "
        "ORDER BY c.created_at DESC LIMIT 1",
    )
    if not rows:
        raise AssertionError("desktop survival continuation not found")
    req, corr, state, attempts, node, reason, token = rows[0]
    hist = query(
        conn,
        "SELECT to_state,attempts FROM todo_bus.continuation_history "
        "WHERE request_id=%s ORDER BY history_id",
        (req,),
    )
    proof = {
        "request_id": str(req),
        "correlation_id": str(corr),
        "state": str(state),
        "attempts": int(attempts),
        "node_id": str(node),
        "reason": str(reason),
        "fencing_token": int(token),
        "history": [[str(s), int(a)] for s, a in hist],
    }
    assert_execution(proof, DESKTOP)
    return proof


def create_continuation(conn: psycopg.Connection, label: str) -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:12]
    corr = f"noteri-physical-cycle-{label}-{suffix}"
    req = f"req-{suffix}"
    idem = hashlib.sha256(corr.encode("utf-8")).hexdigest()
    basis = f"evt-{suffix}"
    payload = json.dumps(
        {
            "automation_action": "ai_control_plane.validate_idempotent_continuation.v1",
            "next_action": "validar continuidade idempotente",
            "external_id": f"desktop-24x7-e2e-{suffix}",
        },
        separators=(",", ":"),
    )
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO todo_bus.continuation_requests("
            "request_id,idempotency_key,basis_event_id,correlation_id,project,todo_payload"
            ") VALUES (%s,%s,%s,%s,%s,%s::jsonb)",
            (req, idem, basis, corr, "AI Control Plane", payload),
        )
    conn.commit()
    return req, corr


def wait_completion(conn: psycopg.Connection, req: str, corr: str, timeout_seconds: int = 75) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    last: list[tuple] = []
    while time.monotonic() < deadline:
        last = query(
            conn,
            "SELECT c.state,c.attempts,coalesce(d.node_id,''),coalesce(d.reason,''),"
            "coalesce(d.fencing_token,0) "
            "FROM todo_bus.continuation_requests c "
            "LEFT JOIN todo_bus.host_route_decisions d ON d.correlation_id=c.correlation_id "
            "WHERE c.request_id=%s",
            (req,),
        )
        if last and str(last[0][0]) in {"COMPLETED", "DLQ", "HUMAN_GATE"}:
            state, attempts, node, reason, token = last[0]
            history = query(
                conn,
                "SELECT to_state,attempts FROM todo_bus.continuation_history "
                "WHERE request_id=%s ORDER BY history_id",
                (req,),
            )
            return {
                "request_id": req,
                "correlation_id": corr,
                "state": str(state),
                "attempts": int(attempts),
                "node_id": str(node),
                "reason": str(reason),
                "fencing_token": int(token),
                "history": [[str(s), int(a)] for s, a in history],
            }
        time.sleep(0.5)
    raise AssertionError(f"completion timeout req={req} last={last}")


def assert_execution(proof: dict[str, object], owner: str) -> None:
    if proof["state"] != "COMPLETED":
        raise AssertionError(f"unexpected state: {proof}")
    if proof["attempts"] != 1:
        raise AssertionError(f"expected one final attempt: {proof}")
    if proof["node_id"] != owner:
        raise AssertionError(f"unexpected owner expected={owner}: {proof}")
    states = [str(item[0]) for item in list(proof["history"])]
    attempts = [int(item[1]) for item in list(proof["history"])]
    if not states or states[0] != "PENDING" or states[-2:] != ["PROCESSING", "COMPLETED"]:
        raise AssertionError(f"unexpected history: {proof}")
    if states.count("COMPLETED") != 1:
        raise AssertionError(f"duplicate completion: {proof}")
    if any(x in {"DLQ", "HUMAN_GATE"} for x in states):
        raise AssertionError(f"unexpected terminal error: {proof}")
    if max(attempts, default=0) > 1:
        raise AssertionError(f"attempt counter exceeded one: {proof}")


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        check=check,
    )


def main() -> int:
    if socket.gethostname().casefold() != DESKTOP.casefold():
        raise SystemExit(f"must run on {DESKTOP}")
    dsn = load_dsn()
    desktop_worker_stopped = False
    with psycopg.connect(dsn, autocommit=False) as conn:
        desktop_survival = latest_desktop_survival(conn)
        save("resume_validated_desktop_survival", desktop_survival=desktop_survival, health=health(conn))
        try:
            after_return = wait_node(
                conn,
                NOTERI,
                True,
                TTL_SECONDS + STABILITY_SECONDS + 600,
                "waiting_noteri_return",
            )
            service_age = wait_service_heartbeat(conn, 300)
            deadline = time.monotonic() + STABILITY_SECONDS + 60
            while int(after_return.get(NOTERI, {}).get("healthy_age_seconds", 0)) < STABILITY_SECONDS and time.monotonic() < deadline:
                time.sleep(1)
                after_return = health(conn)
            if int(after_return.get(NOTERI, {}).get("healthy_age_seconds", 0)) < STABILITY_SECONDS:
                raise AssertionError(f"Noteri failed stability window: {after_return}")
            save("noteri_returned_headless", service_heartbeat_age_seconds=service_age, health=after_return)

            stop = docker("stop", DESKTOP_WORKER_CONTAINER, check=False)
            if stop.returncode != 0:
                raise RuntimeError((stop.stderr or stop.stdout)[-800:])
            desktop_worker_stopped = True

            after_stop = wait_node(
                conn,
                DESKTOP,
                False,
                TTL_SECONDS + 120,
                "waiting_desktop_worker_offline",
            )
            if not bool(after_stop.get(NOTERI, {}).get("healthy")):
                raise AssertionError(f"Noteri unhealthy before takeover: {after_stop}")

            req, corr = create_continuation(conn, "noteri-takeover")
            takeover = wait_completion(conn, req, corr)
            assert_execution(takeover, NOTERI)

            repeated = query(
                conn,
                "SELECT node_id,reason,fencing_token "
                "FROM todo_bus.claim_host_route(%s,%s,%s)",
                (corr, DESKTOP, "noteri_symmetric_repeat_control"),
            )
            if len(repeated) != 1:
                raise AssertionError(f"route repeat returned {repeated}")
            rep_node, rep_reason, rep_token = repeated[0]
            if (
                str(rep_node) != NOTERI
                or str(rep_reason) != str(takeover["reason"])
                or int(rep_token) != int(takeover["fencing_token"])
            ):
                raise AssertionError(f"route idempotency failed: {repeated} vs {takeover}")
            conn.commit()

            start = docker("start", DESKTOP_WORKER_CONTAINER, check=False)
            if start.returncode != 0:
                raise RuntimeError((start.stderr or start.stdout)[-800:])
            desktop_worker_stopped = False
            final_health = wait_node(
                conn,
                DESKTOP,
                True,
                TTL_SECONDS + STABILITY_SECONDS + 120,
                "waiting_desktop_worker_return",
            )
            final_service_age = wait_service_heartbeat(conn, 60)

            result = {
                "status": "ok",
                "database": "Neon",
                "noteri_physical_cycle_observed": True,
                "desktop_survival": desktop_survival,
                "noteri_service_heartbeat_age_seconds": service_age,
                "noteri_takeover": takeover,
                "route_repeat_same_correlation": [str(rep_node), str(rep_reason), int(rep_token)],
                "final_service_heartbeat_age_seconds": final_service_age,
                "final_health": final_health,
                "ttl_seconds": TTL_SECONDS,
                "stability_seconds": STABILITY_SECONDS,
                "single_execution": True,
            }
            save("completed", **result)
            print(json.dumps(result, sort_keys=True))
            return 0
        finally:
            if desktop_worker_stopped:
                docker("start", DESKTOP_WORKER_CONTAINER, check=False)


if __name__ == "__main__":
    raise SystemExit(main())

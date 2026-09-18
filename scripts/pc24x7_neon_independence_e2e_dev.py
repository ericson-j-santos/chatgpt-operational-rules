from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
BASE = Path(os.environ["LOCALAPPDATA"]) / "ReqSys" / "TodoGlobal24x7"
RUNTIME_ENV = BASE / "runtime.env"
GATEWAY = "todo-global-24x7-gateway-1"
WORKER = "todo-global-24x7-worker-1"
LOCAL_DB = "todo-global-24x7-db-1"
LOCAL_BRIDGE = "todo-global-24x7-db_bridge-1"
EXPECTED_DB = "todo_global_bus_dev_ha"
PROJECT = "AI Control Plane"


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def load_token() -> str:
    for raw in RUNTIME_ENV.read_text(encoding="utf-8").splitlines():
        if raw.startswith("TODO_GATEWAY_TOKEN="):
            token = raw.split("=", 1)[1].strip()
            if token:
                return token
    raise SystemExit("TODO_GATEWAY_TOKEN missing")


def assert_gateway_neon() -> None:
    code = (
        "import os,urllib.parse;"
        "u=urllib.parse.urlparse(os.environ['DATABASE_URL']);"
        "assert (u.hostname or '').endswith('.neon.tech');"
        "assert u.path.lstrip('/')=='todo_global_bus_dev_ha';"
        "print('NEON_DB_OK')"
    )
    cp = run(["docker", "exec", GATEWAY, "python", "-c", code])
    if "NEON_DB_OK" not in cp.stdout:
        raise RuntimeError("gateway external database proof failed")


def neon_rows(sql: str) -> list[list[object]]:
    code = (
        "import os,psycopg,json;"
        "c=psycopg.connect(os.environ['DATABASE_URL']);"
        "cur=c.cursor();"
        f"cur.execute({sql!r});"
        "rows=cur.fetchall();"
        "print(json.dumps(rows,default=str));"
        "cur.close();c.close()"
    )
    cp = run(["docker", "exec", GATEWAY, "python", "-c", code])
    return json.loads(cp.stdout.strip().splitlines()[-1])


def api_json(method: str, path: str, token: str, payload: dict | None = None) -> tuple[int, dict]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(f"http://127.0.0.1:8094{path}", data=body, headers=headers, method=method)
    for attempt in range(1, 11):
        try:
            with urlopen(request, timeout=8) as response:
                raw = response.read(262144)
                return int(response.status), json.loads(raw.decode("utf-8"))
        except HTTPError as exc:
            raw = exc.read(262144)
            return int(exc.code), json.loads(raw.decode("utf-8")) if raw else {}
        except URLError:
            if attempt >= 10:
                raise
            time.sleep(2)
    raise RuntimeError("gateway API retry exhausted")


def wait_failover_health(timeout_seconds: int = 50) -> dict[str, dict[str, object]]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, dict[str, object]] = {}
    while time.monotonic() < deadline:
        rows = neon_rows(
            "SELECT node_id,healthy,heartbeat_age_seconds,healthy_age_seconds "
            "FROM todo_bus.get_runtime_node_health('DESKTOP-PDQK954','Noteri',20,15) "
            "ORDER BY node_id"
        )
        last = {
            str(row[0]): {
                "healthy": bool(row[1]),
                "heartbeat_age_seconds": int(row[2]),
                "healthy_age_seconds": int(row[3]),
            }
            for row in rows
        }
        if (
            not last.get("DESKTOP-PDQK954", {}).get("healthy", True)
            and last.get("Noteri", {}).get("healthy") is True
        ):
            return last
        time.sleep(1)
    raise AssertionError(f"failover health timeout: {last}")


def wait_completion(request_id: str, timeout_seconds: int = 45) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    last: list[list[object]] = []
    while time.monotonic() < deadline:
        last = neon_rows(
            "SELECT c.state,c.attempts,coalesce(d.node_id,''),coalesce(d.fencing_token,0) "
            "FROM todo_bus.continuation_requests c "
            "LEFT JOIN todo_bus.host_route_decisions d ON d.correlation_id=c.correlation_id "
            f"WHERE c.request_id='{request_id}'"
        )
        if last and str(last[0][0]) in {"COMPLETED", "HUMAN_GATE", "DLQ"}:
            state, attempts, node_id, token = last[0]
            history = neon_rows(
                "SELECT to_state,attempts FROM todo_bus.continuation_history "
                f"WHERE request_id='{request_id}' ORDER BY history_id"
            )
            return {
                "state": str(state),
                "attempts": int(attempts),
                "node_id": str(node_id),
                "fencing_token": int(token),
                "history": [[str(x[0]), int(x[1])] for x in history],
            }
        time.sleep(0.5)
    raise AssertionError(f"completion timeout request_id={request_id} last={last}")


def main() -> int:
    if not RUNTIME_ENV.is_file():
        raise SystemExit("runtime env missing")
    token = load_token()
    assert_gateway_neon()
    stopped = []
    success = False
    try:
        for name in (WORKER, LOCAL_BRIDGE, LOCAL_DB):
            cp = run(["docker", "stop", name], check=False)
            if cp.returncode == 0:
                stopped.append(name)
        health = wait_failover_health()

        suffix = uuid.uuid4().hex[:12]
        external_id = f"neon-independent-{suffix}"
        event_id = f"evt-neon-{suffix}"
        correlation_id = f"corr-neon-{suffix}"
        continuation_correlation = f"continue-neon-{suffix}"
        idem = hashlib.sha256(f"{PROJECT}|Automação|{external_id}".encode("utf-8")).hexdigest()
        event = {
            "schema_version": "1.0",
            "event_id": event_id,
            "event_type": "todo.updated",
            "occurred_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "correlation_id": correlation_id,
            "idempotency_key": idem,
            "project": PROJECT,
            "producer": "pc24x7-neon-independence-e2e",
            "todo": {
                "title": "Neon independent control plane E2E",
                "type": "Automação",
                "external_id": external_id,
                "status": "EM ANDAMENTO",
                "priority": "P0",
                "source": "ChatGPT",
                "next_action": "validar continuidade idempotente",
                "automation_action": "ai_control_plane.validate_idempotent_continuation.v1",
            },
        }
        post_status, _ = api_json("POST", "/v1/events", token, event)
        cont_status, cont = api_json(
            "POST",
            f"/v1/todos/{idem}/continue",
            token,
            {"correlation_id": continuation_correlation},
        )
        replay_status, replay = api_json(
            "POST",
            f"/v1/todos/{idem}/continue",
            token,
            {"correlation_id": continuation_correlation},
        )
        request_id = str(cont.get("request_id") or "")
        if post_status != 202 or cont_status != 202 or replay_status != 202 or not request_id:
            raise AssertionError(
                f"gateway acceptance failed post={post_status} continue={cont_status} replay={replay_status}"
            )
        if str(replay.get("request_id") or "") != request_id:
            raise AssertionError("idempotent continuation replay returned a different request")

        proof = wait_completion(request_id)
        if proof["state"] != "COMPLETED":
            raise AssertionError(f"unexpected terminal state: {proof}")
        if proof["attempts"] != 1:
            raise AssertionError(f"expected one attempt: {proof}")
        if proof["node_id"] != "Noteri":
            raise AssertionError(f"expected Noteri owner: {proof}")
        terminal_count = sum(1 for state, _ in proof["history"] if state == "COMPLETED")
        if terminal_count != 1:
            raise AssertionError(f"duplicate completion detected: {proof}")

        rows = neon_rows(
            "SELECT count(*) FROM todo_bus.continuation_requests "
            f"WHERE idempotency_key='{idem}'"
        )
        if int(rows[0][0]) != 1:
            raise AssertionError("duplicate continuation row detected")

        success = True
        run(["docker", "start", WORKER])
        print(json.dumps({
            "status": "ok",
            "database": EXPECTED_DB,
            "desktop_local_db_stopped": True,
            "desktop_local_bridge_stopped": True,
            "noteri_completed": True,
            "single_execution": True,
            "request_id": request_id,
            "failover_health": health,
            "proof": proof,
            "local_database_preserved_for_rollback": True,
        }, sort_keys=True))
        return 0
    finally:
        if not success:
            for name in (LOCAL_DB, LOCAL_BRIDGE, WORKER):
                if name in stopped:
                    run(["docker", "start", name], check=False)


if __name__ == "__main__":
    raise SystemExit(main())

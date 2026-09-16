#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from scripts.todo_event_bus import make_idempotency_key, utc_now_iso

ENV_FILE = Path("/etc/todo-global/runtime.env")
EVIDENCE_DIR = Path("/var/lib/todo-global/evidence")
PROJECT = "AI Control Plane"
DB_NAME = "todo_global_bus_dev"
SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]+$")
SAFE_HEX = re.compile(r"^[0-9a-f]{64}$")


def require_root() -> None:
    if os.name != "posix" or os.geteuid() != 0:
        raise RuntimeError("E2E Debian must run as root for independent PostgreSQL readback")


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if raw and not raw.lstrip().startswith("#") and "=" in raw:
            key, value = raw.split("=", 1)
            values[key.strip()] = value.strip()
    if not values.get("TODO_GATEWAY_TOKEN"):
        raise RuntimeError("TODO_GATEWAY_TOKEN missing")
    return values


def api_json(method: str, path: str, token: str, payload: dict | None = None) -> tuple[int, dict]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(f"http://127.0.0.1:8000{path}", data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=8) as response:
            return int(response.status), json.loads(response.read(262144).decode("utf-8"))
    except HTTPError as exc:
        raw = exc.read(262144)
        return int(exc.code), json.loads(raw.decode("utf-8")) if raw else {}


def psql_scalar(sql: str) -> str:
    result = subprocess.run(
        ["runuser", "-u", "postgres", "--", "psql", "-d", DB_NAME, "-At", "-c", sql],
        text=True, capture_output=True, timeout=30, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("independent PostgreSQL readback failed")
    return result.stdout.strip()


def main() -> int:
    require_root()
    token = load_env()["TODO_GATEWAY_TOKEN"]
    suffix = uuid.uuid4().hex[:12]
    external_id = f"debian-24x7-e2e-{suffix}"
    event_id = f"evt-debian-{suffix}"
    correlation_id = f"corr-debian-24x7-{suffix}"
    continuation_correlation = f"continue-debian-24x7-{suffix}"
    key = make_idempotency_key(PROJECT, "Automação", external_id)
    if not SAFE_ID.fullmatch(event_id) or not SAFE_HEX.fullmatch(key):
        raise RuntimeError("generated identifiers failed validation")

    event = {
        "schema_version": "1.0",
        "event_id": event_id,
        "event_type": "todo.updated",
        "occurred_at": utc_now_iso(),
        "correlation_id": correlation_id,
        "idempotency_key": key,
        "project": PROJECT,
        "producer": "debian-24x7-e2e",
        "todo": {
            "title": "Control Plane Debian 24x7 E2E",
            "type": "Automação",
            "external_id": external_id,
            "status": "EM ANDAMENTO",
            "priority": "P0",
            "source": "ChatGPT",
            "next_action": "validar continuidade idempotente",
        },
    }

    post_status, first = api_json("POST", "/v1/events", token, event)
    replay_status, replay = api_json("POST", "/v1/events", token, event)
    todos_status, todos = api_json("GET", f"/v1/todos?{urlencode({'project': PROJECT, 'limit': 200})}", token)
    todo_match = next((item for item in todos.get("items", []) if item.get("idempotency_key") == key), None)
    cont_status, cont = api_json("POST", f"/v1/todos/{key}/continue", token, {"correlation_id": continuation_correlation})
    cont_replay_status, cont_replay = api_json("POST", f"/v1/todos/{key}/continue", token, {"correlation_id": continuation_correlation})
    conts_status, conts = api_json("GET", "/v1/continuations?limit=200", token)
    cont_match = next((item for item in conts.get("items", []) if item.get("idempotency_key") == key), None)

    event_sql = psql_scalar(
        "SELECT event_id || '|' || state || '|' || idempotency_key "
        f"FROM todo_bus.queue_events WHERE event_id = '{event_id}'"
    )
    continuation_sql = psql_scalar(
        "SELECT request_id || '|' || state || '|' || basis_event_id "
        f"FROM todo_bus.continuation_requests WHERE idempotency_key = '{key}' ORDER BY created_at DESC LIMIT 1"
    )

    ready = all([
        post_status == 202, first.get("accepted") is True, first.get("duplicate") is False,
        replay_status == 202, replay.get("duplicate") is True,
        todos_status == 200, todo_match is not None,
        cont_status == 202, cont.get("duplicate") is False,
        cont_replay_status == 202, cont_replay.get("duplicate") is True,
        cont_replay.get("request_id") == cont.get("request_id"),
        conts_status == 200, cont_match is not None,
        event_sql.startswith(f"{event_id}|") and event_sql.endswith(f"|{key}"),
        continuation_sql.endswith(f"|{event_id}"),
    ])
    evidence = {
        "contract": "todo-global-debian-live-e2e",
        "generated_at": datetime.now(UTC).isoformat(),
        "host_role": "debian-vm-primary",
        "event_id": event_id,
        "idempotency_key": key,
        "request_id": cont.get("request_id"),
        "post_accepted": post_status == 202 and first.get("duplicate") is False,
        "event_replay_idempotent": replay_status == 202 and replay.get("duplicate") is True,
        "todo_readback": todos_status == 200 and todo_match is not None,
        "continuation_created": cont_status == 202 and cont.get("duplicate") is False,
        "continuation_replay_idempotent": cont_replay_status == 202 and cont_replay.get("duplicate") is True,
        "continuation_readback": conts_status == 200 and cont_match is not None,
        "sql_event_readback": event_sql.startswith(f"{event_id}|") and event_sql.endswith(f"|{key}"),
        "sql_continuation_readback": continuation_sql.endswith(f"|{event_id}"),
        "ready": ready,
    }
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / "e2e-last.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence))
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())

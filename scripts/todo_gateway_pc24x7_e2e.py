#!/usr/bin/env python3
"""E2E real do Control Plane 24x7, com leitura SQL independente e sem expor segredos."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from scripts.todo_event_bus import make_idempotency_key, utc_now_iso
from scripts.todo_gateway_pc24x7 import PORT, runtime_dir

PROJECT = "AI Control Plane"
DB_CONTAINER = "todo-global-24x7-db-1"
DB_USER = "todo_global_bus_dev_user"
DB_NAME = "todo_global_bus_dev"
SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]+$")
SAFE_HEX = re.compile(r"^[0-9a-f]{64}$")


def load_runtime_env() -> dict[str, str]:
    path = runtime_dir() / "runtime.env"
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw or raw.lstrip().startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        values[key.strip()] = value.strip()
    token = values.get("TODO_GATEWAY_TOKEN", "")
    if not token:
        raise RuntimeError("TODO_GATEWAY_TOKEN missing from local runtime env")
    return values


def docker_executable() -> str:
    discovered = shutil.which("docker")
    if discovered:
        return discovered
    if os.name == "nt":
        candidate = Path.home() / "AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe"
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError("docker CLI not found in PATH or known Docker Desktop location")


def api_json(method: str, path: str, token: str, payload: dict | None = None) -> tuple[int, dict]:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(f"http://127.0.0.1:{PORT}{path}", data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=8) as response:
            raw = response.read(262144)
            return int(response.status), json.loads(raw.decode("utf-8"))
    except HTTPError as exc:
        raw = exc.read(262144)
        payload_out = json.loads(raw.decode("utf-8")) if raw else {}
        return int(exc.code), payload_out


def psql_scalar(sql: str) -> str:
    result = subprocess.run(
        [docker_executable(), "exec", DB_CONTAINER, "psql", "-U", DB_USER, "-d", DB_NAME, "-At", "-c", sql],
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip()[-500:]
        raise RuntimeError(f"independent SQL read failed: {detail or 'docker exec returned non-zero'}")
    return result.stdout.strip()


def main() -> int:
    env = load_runtime_env()
    token = env["TODO_GATEWAY_TOKEN"]
    suffix = uuid.uuid4().hex[:12]
    external_id = f"desktop-24x7-e2e-{suffix}"
    event_id = f"evt-desktop-{suffix}"
    correlation_id = f"corr-desktop-24x7-{suffix}"
    continuation_correlation = f"continue-desktop-24x7-{suffix}"
    key = make_idempotency_key(PROJECT, "Automação", external_id)
    if not SAFE_ID.fullmatch(event_id) or not SAFE_HEX.fullmatch(key):
        raise RuntimeError("generated identifiers failed safety validation")

    event = {
        "schema_version": "1.0",
        "event_id": event_id,
        "event_type": "todo.updated",
        "occurred_at": utc_now_iso(),
        "correlation_id": correlation_id,
        "idempotency_key": key,
        "project": PROJECT,
        "producer": "pc24x7-desktop-e2e",
        "todo": {
            "title": "Control Plane Desktop 24x7 E2E",
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
    query = urlencode({"project": PROJECT, "limit": 200})
    todos_status, todos = api_json("GET", f"/v1/todos?{query}", token)
    todo_match = next((item for item in todos.get("items", []) if item.get("idempotency_key") == key), None)

    cont_status, cont = api_json(
        "POST",
        f"/v1/todos/{key}/continue",
        token,
        {"correlation_id": continuation_correlation},
    )
    cont_replay_status, cont_replay = api_json(
        "POST",
        f"/v1/todos/{key}/continue",
        token,
        {"correlation_id": continuation_correlation},
    )
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

    ok = all(
        [
            post_status == 202,
            first.get("accepted") is True,
            first.get("duplicate") is False,
            replay_status == 202,
            replay.get("duplicate") is True,
            todos_status == 200,
            todo_match is not None,
            cont_status == 202,
            cont.get("duplicate") is False,
            cont_replay_status == 202,
            cont_replay.get("duplicate") is True,
            cont_replay.get("request_id") == cont.get("request_id"),
            conts_status == 200,
            cont_match is not None,
            event_sql.startswith(f"{event_id}|"),
            event_sql.endswith(f"|{key}"),
            continuation_sql.endswith(f"|{event_id}"),
        ]
    )

    evidence = {
        "contract": "todo-global-pc24x7-live-e2e",
        "generated_at": datetime.now(UTC).isoformat(),
        "host_role": "desktop-primary",
        "port": PORT,
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
        "ready": ok,
    }
    evidence_dir = runtime_dir() / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "e2e-last.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(json.dumps(evidence, ensure_ascii=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""E2E isolado TODO continuation -> execution lane HTTP -> replay sem duplicidade."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.continuation_worker import Continuation, HumanGate, execute  # noqa: E402


class LaneState:
    def __init__(self, token: str) -> None:
        self.token = token
        self.requests = 0
        self.tasks: dict[tuple[str, int, str], dict[str, Any]] = {}


class Handler(BaseHTTPRequestHandler):
    server_version = "ExecutionLaneE2E/1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:
        state: LaneState = self.server.state  # type: ignore[attr-defined]
        if self.path != "/v1/tasks":
            self.send_error(404)
            return
        if self.headers.get("Authorization") != f"Bearer {state.token}":
            self.send_error(401)
            return
        size = int(self.headers.get("Content-Length") or "0")
        payload = json.loads(self.rfile.read(size).decode("utf-8"))
        state.requests += 1
        key = (payload["repository"], int(payload["issue_number"]), payload["request_id"])
        created = key not in state.tasks
        if created:
            state.tasks[key] = {
                **payload,
                "task_id": f"e2e-task-{len(state.tasks) + 1}",
                "state": "queued",
            }
        body = json.dumps({"created": created, "task": state.tasks[key]}).encode("utf-8")
        self.send_response(201 if created else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def continuation(request_id: str) -> Continuation:
    return Continuation(
        request_id="cont-" + request_id,
        idempotency_key="a" * 64,
        correlation_id="corr-execution-lane-e2e",
        project="E2E",
        todo={
            "automation_action": "execution_lane.enqueue.v1",
            "external_id": request_id,
            "execution_request": {
                "repository": "ericson-j-santos/example",
                "issue_number": 90,
                "request_id": request_id,
                "base_sha": "b" * 40,
                "priority": 10,
            },
        },
        attempts=1,
    )


def main() -> int:
    token = "e2e-token-not-persisted"
    state = LaneState(token)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.state = state  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    previous_url = os.environ.get("EXECUTION_LANE_BASE_URL")
    previous_token = os.environ.get("EXECUTION_LANE_API_TOKEN_FILE")
    try:
        with tempfile.TemporaryDirectory(prefix="execution-lane-e2e-") as tmp:
            token_file = Path(tmp) / "token"
            token_file.write_text(token + "\n", encoding="utf-8")
            os.environ["EXECUTION_LANE_BASE_URL"] = f"http://127.0.0.1:{server.server_port}"
            os.environ["EXECUTION_LANE_API_TOKEN_FILE"] = str(token_file)

            item = continuation("stable-request-90")
            execute(item)
            execute(item)

            before_negative = state.requests
            bad = continuation("negative-request-90")
            bad = Continuation(
                bad.request_id,
                bad.idempotency_key,
                bad.correlation_id,
                bad.project,
                {**bad.todo, "external_id": "different-id"},
                bad.attempts,
            )
            negative_blocked = False
            try:
                execute(bad)
            except HumanGate:
                negative_blocked = True

            key = ("ericson-j-santos/example", 90, "stable-request-90")
            stored = state.tasks.get(key)
            passed = all(
                [
                    state.requests == 2,
                    len(state.tasks) == 1,
                    stored is not None,
                    stored.get("base_sha") == "b" * 40 if stored else False,
                    negative_blocked,
                    state.requests == before_negative,
                ]
            )
            result = {
                "overall_passed": passed,
                "http_requests": state.requests,
                "logical_tasks": len(state.tasks),
                "replay_deduplicated": len(state.tasks) == 1,
                "negative_blocked_before_io": negative_blocked and state.requests == before_negative,
                "independent_task_id": stored.get("task_id") if stored else None,
            }
            print(json.dumps(result, sort_keys=True))
            return 0 if passed else 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        if previous_url is None:
            os.environ.pop("EXECUTION_LANE_BASE_URL", None)
        else:
            os.environ["EXECUTION_LANE_BASE_URL"] = previous_url
        if previous_token is None:
            os.environ.pop("EXECUTION_LANE_API_TOKEN_FILE", None)
        else:
            os.environ["EXECUTION_LANE_API_TOKEN_FILE"] = previous_token


if __name__ == "__main__":
    raise SystemExit(main())

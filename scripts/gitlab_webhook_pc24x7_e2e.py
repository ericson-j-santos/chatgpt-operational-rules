#!/usr/bin/env python3
from __future__ import annotations

import json
import uuid
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from todo_gateway_pc24x7_e2e import load_runtime_env

URL = "http://127.0.0.1:8094/v1/webhooks/gitlab"


def post(payload: dict, token: str, event_uuid: str) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8")
    request = Request(URL, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "X-Gitlab-Token": token,
        "X-Gitlab-Event": "Push Hook",
        "X-Gitlab-Event-UUID": event_uuid,
    })
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload_out = json.loads(raw)
        except json.JSONDecodeError:
            payload_out = {}
        return exc.code, payload_out

def main() -> int:
    env = load_runtime_env()
    token = env.get("GITLAB_WEBHOOK_TOKEN", "")
    project = env.get("GITLAB_WEBHOOK_PROJECT", "")
    if not token or not project:
        raise SystemExit("configuração GitLab webhook ausente")
    event_uuid = f"pc24x7-e2e-{uuid.uuid4()}"
    payload = {
        "object_kind": "push",
        "after": uuid.uuid4().hex,
        "ref": "refs/heads/e2e-webhook",
        "project": {"id": 84366761, "path_with_namespace": project},
    }
    bad_status, _ = post(payload, "invalid-token", event_uuid)
    first_status, first = post(payload, token, event_uuid)
    replay_status, replay = post(payload, token, event_uuid)
    ok = (
        bad_status == 401
        and first_status == 202
        and replay_status == 202
        and first.get("duplicate") is False
        and replay.get("duplicate") is True
        and first.get("event_id") == replay.get("event_id")
    )
    print(json.dumps({
        "result": "PASS" if ok else "FAIL",
        "invalid_status": bad_status,
        "first_status": first_status,
        "replay_status": replay_status,
        "duplicate_first": first.get("duplicate"),
        "duplicate_replay": replay.get("duplicate"),
        "event_id": first.get("event_id"),
        "correlation_id": first.get("correlation_id"),
    }, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

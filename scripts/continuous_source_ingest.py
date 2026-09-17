#!/usr/bin/env python3
"""Ingestão contínua Teams/GitLab -> TodoEvent -> TODO Gateway."""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from scripts.source_todo_adapter import adapt_external_source
from scripts.todo_gateway_pc24x7 import PORT, runtime_dir
from scripts.todo_gateway_pc24x7_e2e import load_runtime_env

GRAPH = "https://graph.microsoft.com/v1.0"
TAG_RE = re.compile(r"<[^>]+>")


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def clean_title(value: str, fallback: str) -> str:
    text = TAG_RE.sub(" ", html.unescape(value or ""))
    text = " ".join(text.split())
    return (text or fallback)[:180]


def request_json(method: str, url: str, headers: dict[str, str] | None = None,
                 body: bytes | None = None, timeout: int = 15) -> tuple[int, dict]:
    request = Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(524288)
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            return int(response.status), payload
    except HTTPError as exc:
        raw = exc.read(524288)
        payload = json.loads(raw.decode("utf-8")) if raw else {}
        return int(exc.code), payload


def required_env(*names: str) -> tuple[dict[str, str], list[str]]:
    values: dict[str, str] = {}
    missing: list[str] = []
    for name in names:
        value = str(os.environ.get(name, "")).strip()
        if value:
            values[name] = value
        else:
            missing.append(name)
    return values, missing


def teams_token() -> str:
    values, missing = required_env(
        "POWER_PLATFORM_TENANT_ID", "POWER_PLATFORM_CLIENT_ID", "POWER_PLATFORM_CLIENT_SECRET"
    )
    if missing:
        raise RuntimeError("teams_config_missing:" + ",".join(missing))
    tenant = quote(values["POWER_PLATFORM_TENANT_ID"], safe="")
    body = urlencode({
        "client_id": values["POWER_PLATFORM_CLIENT_ID"],
        "client_secret": values["POWER_PLATFORM_CLIENT_SECRET"],
        "grant_type": "client_credentials",
        "scope": "https://graph.microsoft.com/.default",
    }).encode("utf-8")
    status, payload = request_json(
        "POST",
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        {"Content-Type": "application/x-www-form-urlencoded"},
        body,
    )
    if status != 200 or not payload.get("access_token"):
        raise RuntimeError(f"teams_token_http_{status}")
    return str(payload["access_token"])


def fetch_teams_messages() -> list[dict]:
    values, missing = required_env("PLANNER_TEAMS_DEV_TEAM_ID", "PLANNER_TEAMS_DEV_CHANNEL_ID")
    if missing:
        raise RuntimeError("teams_config_missing:" + ",".join(missing))
    token = teams_token()
    team = quote(values["PLANNER_TEAMS_DEV_TEAM_ID"], safe="")
    channel = quote(values["PLANNER_TEAMS_DEV_CHANNEL_ID"], safe="")
    url = f"{GRAPH}/teams/{team}/channels/{channel}/messages?$top=50"
    status, payload = request_json("GET", url, {
        "Authorization": f"Bearer {token}", "Accept": "application/json"
    })
    if status == 403:
        raise RuntimeError("graph_teams_read_forbidden")
    if status != 200:
        raise RuntimeError(f"teams_messages_http_{status}")
    return list(payload.get("value") or [])


def fetch_gitlab_commits() -> list[dict]:
    project_id = str(os.environ.get("GITLAB_PROJECT_ID", "84366761")).strip()
    token = str(os.environ.get("GITLAB_TOKEN", "")).strip()
    if not token:
        raise RuntimeError("gitlab_config_missing:GITLAB_TOKEN")
    api = str(os.environ.get("GITLAB_API_URL", "https://gitlab.com/api/v4")).rstrip("/")
    project = quote(project_id, safe="")
    headers = {"Accept": "application/json"}
    if token:
        headers["PRIVATE-TOKEN"] = token
    status, payload = request_json(
        "GET", f"{api}/projects/{project}/repository/commits?per_page=20", headers
    )
    if status in {401, 403, 404}:
        raise RuntimeError(f"gitlab_access_http_{status}")
    if status != 200 or not isinstance(payload, list):
        raise RuntimeError(f"gitlab_commits_http_{status}")
    return payload


def state_path() -> Path:
    return runtime_dir() / "continuous-source-state.json"


def load_state() -> dict[str, list[str]]:
    path = state_path()
    if not path.exists():
        return {"teams": [], "gitlab": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {"teams": list(data.get("teams") or []), "gitlab": list(data.get("gitlab") or [])}


def save_state(state: dict[str, list[str]]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def todo_token() -> str:
    token = str(os.environ.get("TODO_GATEWAY_TOKEN", "")).strip()
    if token:
        return token
    return load_runtime_env()["TODO_GATEWAY_TOKEN"]


def publish_event(payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    status, response = request_json(
        "POST",
        f"http://127.0.0.1:{PORT}/v1/events",
        {
            "Authorization": f"Bearer {todo_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        body,
    )
    if status != 202:
        raise RuntimeError(f"todo_gateway_http_{status}")
    return response


def teams_event(message: dict):
    message_id = str(message.get("id") or "").strip()
    if not message_id:
        raise ValueError("teams_message_id_missing")
    body = message.get("body") if isinstance(message.get("body"), dict) else {}
    title = clean_title(str(body.get("content") or ""), f"Teams message {message_id}")
    return adapt_external_source("teams", {
        "external_id": f"message-{message_id}",
        "title": title,
        "occurred_at": message.get("createdDateTime") or now_iso(),
        "status": "PENDENTE",
        "priority": "P1",
    })


def gitlab_event(commit: dict):
    commit_id = str(commit.get("id") or "").strip()
    if not commit_id:
        raise ValueError("gitlab_commit_id_missing")
    title = clean_title(str(commit.get("title") or ""), f"GitLab commit {commit_id[:12]}")
    return adapt_external_source("gitlab", {
        "external_id": f"commit-{commit_id}",
        "title": title,
        "occurred_at": commit.get("committed_date") or commit.get("created_at") or now_iso(),
        "status": "PENDENTE",
        "priority": "P1",
    })


def select_new(items: list[dict], seen: list[str], id_field: str, backfill: bool) -> list[dict]:
    unseen = [item for item in items if str(item.get(id_field) or "") not in set(seen)]
    if not seen and not backfill and unseen:
        return unseen[:1]
    return list(reversed(unseen))


def process_source(source: str, state: dict[str, list[str]], backfill: bool) -> dict:
    if source == "teams":
        items = fetch_teams_messages()
        id_field = "id"
        adapter = teams_event
    elif source == "gitlab":
        items = fetch_gitlab_commits()
        id_field = "id"
        adapter = gitlab_event
    else:
        raise ValueError("source_not_supported")

    first_sync = not state[source] and not backfill
    selected = select_new(items, state[source], id_field, backfill)
    published = 0
    duplicate = 0
    processed_ids: list[str] = []
    for item in selected:
        event = adapter(item)
        response = publish_event(event.to_dict())
        processed_ids.append(str(item.get(id_field) or ""))
        if response.get("duplicate") is True:
            duplicate += 1
        else:
            published += 1

    seed_ids = [str(item.get(id_field) or "") for item in items] if first_sync else processed_ids
    combined = seed_ids + state[source]
    state[source] = list(dict.fromkeys(x for x in combined if x))[:500]
    return {
        "source": source,
        "fetched": len(items),
        "selected": len(selected),
        "published": published,
        "duplicate": duplicate,
    }


def preflight() -> dict:
    _, teams_missing = required_env(
        "POWER_PLATFORM_TENANT_ID", "POWER_PLATFORM_CLIENT_ID",
        "POWER_PLATFORM_CLIENT_SECRET", "PLANNER_TEAMS_DEV_TEAM_ID",
        "PLANNER_TEAMS_DEV_CHANNEL_ID",
    )
    gitlab_missing = [] if str(os.environ.get("GITLAB_TOKEN", "")).strip() else ["GITLAB_TOKEN"]
    todo_ready = True
    try:
        bool(todo_token())
    except (OSError, RuntimeError, KeyError):
        todo_ready = False
    return {
        "contract": "continuous-source-ingest-preflight-v1",
        "todo_gateway_configured": todo_ready,
        "teams_configured": not teams_missing,
        "teams_missing": teams_missing,
        "gitlab_configured": not gitlab_missing,
        "gitlab_missing": gitlab_missing,
        "gitlab_token_present": bool(os.environ.get("GITLAB_TOKEN")),
    }


def run_once(backfill: bool = False) -> dict:
    state = load_state()
    results = []
    errors = []
    for source in ("teams", "gitlab"):
        try:
            results.append(process_source(source, state, backfill))
        except (RuntimeError, ValueError, URLError, OSError, json.JSONDecodeError) as exc:
            errors.append({"source": source, "error": str(exc)[:240]})
    save_state(state)
    return {
        "contract": "continuous-source-ingest-run-v1",
        "generated_at": now_iso(),
        "results": results,
        "errors": errors,
        "ready": not errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingestão contínua Teams/GitLab")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=60)
    args = parser.parse_args()
    if args.preflight:
        print(json.dumps(preflight(), ensure_ascii=False, indent=2))
        return 0
    if args.interval_seconds < 30 or args.interval_seconds > 3600:
        raise SystemExit("interval_seconds deve ficar entre 30 e 3600")
    if args.once:
        result = run_once(args.backfill)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ready"] else 2
    while True:
        result = run_once(args.backfill)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())

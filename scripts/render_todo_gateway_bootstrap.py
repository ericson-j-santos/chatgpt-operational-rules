#!/usr/bin/env python3
"""Securely bootstrap TODO Gateway Render environment without printing secrets."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Iterable

API_BASE = os.environ.get("RENDER_API_BASE", "https://api.render.com/v1").rstrip("/")
TERMINAL_FAILURES = {
    "build_failed",
    "update_failed",
    "canceled",
    "deactivated",
    "pre_deploy_failed",
}


def _request_json(
    method: str,
    path: str,
    token: str,
    payload: Any | None = None,
) -> Any:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{API_BASE}{path}",
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "todo-gateway-bootstrap/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Render API {method} {path} failed with HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Render API {method} {path} transport failure") from exc
    return json.loads(raw.decode("utf-8")) if raw else {}


def _walk_strings(value: Any, path: tuple[str, ...] = ()) -> Iterable[tuple[tuple[str, ...], str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk_strings(child, (*path, str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_strings(child, (*path, str(index)))
    elif isinstance(value, str):
        yield path, value


def select_internal_connection_string(payload: Any) -> str:
    candidates: list[tuple[int, str]] = []
    for path, value in _walk_strings(payload):
        if not value.startswith(("postgres://", "postgresql://")):
            continue
        joined = ".".join(path).lower()
        score = 0
        if "internal" in joined or "private" in joined:
            score += 100
        if "connection" in joined or "url" in joined:
            score += 10
        candidates.append((score, value))
    if not candidates:
        raise RuntimeError("Render connection-info returned no PostgreSQL URL")
    candidates.sort(key=lambda item: item[0], reverse=True)
    best_score = candidates[0][0]
    best = {value for score, value in candidates if score == best_score}
    if best_score < 100 or len(best) != 1:
        raise RuntimeError("Could not identify one unambiguous internal PostgreSQL URL")
    return next(iter(best))


def set_env_var(token: str, service_id: str, key: str, value: str) -> None:
    _request_json(
        "PUT",
        f"/services/{service_id}/env-vars/{key}",
        token,
        {"value": value},
    )


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required secret/config is missing: {name}")
    return value


def _poll_deploy(token: str, service_id: str, deploy_id: str, timeout_seconds: int) -> str:
    deadline = time.monotonic() + timeout_seconds
    last_status = "unknown"
    while time.monotonic() < deadline:
        deploy = _request_json("GET", f"/services/{service_id}/deploys/{deploy_id}", token)
        last_status = str(deploy.get("status", "unknown"))
        if last_status == "live":
            return last_status
        if last_status in TERMINAL_FAILURES:
            raise RuntimeError(f"Render deploy ended in terminal failure: {last_status}")
        time.sleep(10)
    raise RuntimeError(f"Render deploy did not become live before timeout; last_status={last_status}")


def _check_readyz(url: str) -> None:
    request = urllib.request.Request(url.rstrip("/") + "/readyz", headers={"User-Agent": "todo-gateway-bootstrap/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"/readyz failed with HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("/readyz transport failure") from exc
    if status != 200:
        raise RuntimeError(f"/readyz returned HTTP {status}")


def main() -> int:
    token = _require("RENDER_API_KEY")
    service_id = _require("RENDER_SERVICE_ID")
    postgres_id = _require("RENDER_POSTGRES_ID")
    notion_token = _require("NOTION_TOKEN")
    notion_data_source_id = _require("NOTION_DATA_SOURCE_ID")
    service_url = _require("TODO_GATEWAY_URL")
    timeout_seconds = int(os.environ.get("RENDER_DEPLOY_TIMEOUT_SECONDS", "900"))

    connection_info = _request_json("GET", f"/postgres/{postgres_id}/connection-info", token)
    database_url = select_internal_connection_string(connection_info)

    set_env_var(token, service_id, "DATABASE_URL", database_url)
    set_env_var(token, service_id, "NOTION_TOKEN", notion_token)
    set_env_var(token, service_id, "NOTION_DATA_SOURCE_ID", notion_data_source_id)

    deploy = _request_json(
        "POST",
        f"/services/{service_id}/deploys",
        token,
        {"clearCache": "do_not_clear"},
    )
    deploy_id = str(deploy.get("id", "")).strip()
    if not deploy_id:
        raise RuntimeError("Render deploy response did not include deploy id")

    status = _poll_deploy(token, service_id, deploy_id, timeout_seconds)
    _check_readyz(service_url)
    print(
        "RENDER_TODO_BOOTSTRAP_OK "
        f"service_id={service_id} postgres_id={postgres_id} deploy_id={deploy_id} "
        f"deploy_status={status} readyz=200 env_keys=DATABASE_URL,NOTION_TOKEN,NOTION_DATA_SOURCE_ID"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - fail closed at process boundary.
        print(f"RENDER_TODO_BOOTSTRAP_FAILED {exc}", file=sys.stderr)
        raise SystemExit(1)

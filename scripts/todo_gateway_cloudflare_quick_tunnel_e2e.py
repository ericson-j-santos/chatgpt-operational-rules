#!/usr/bin/env python3
"""E2E temporário de HTTPS público para o webhook GitLab no PC24x7.

Cria um Quick Tunnel Cloudflare somente durante a validação, prova autenticação,
idempotência e persistência SQL e remove exclusivamente o container criado ao final.
Nenhum segredo é impresso ou persistido em Git.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CLOUDFLARED_IMAGE = "cloudflare/cloudflared:2026.9.1"
DOCKER_NETWORK = "todo-global-24x7_default"
GATEWAY_ORIGIN = "http://gateway:8000"
PROJECT = "ericson-j-santos/reqsys-v2-enterprise-real"
DB_CONTAINER = "todo-global-24x7-db-1"
DB_USER = "todo_global_bus_dev_user"
DB_NAME = "todo_global_bus_dev"
TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.I)


def runtime_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "ReqSys" / "TodoGlobal24x7"
    return Path.home() / ".config" / "todo-global-24x7"


def docker() -> str:
    value = shutil.which("docker")
    if value:
        return value
    raise RuntimeError("docker CLI not found")


def run(*args: str, timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess[str]:
    cp = subprocess.run(
        [docker(), *args], text=True, capture_output=True, encoding="utf-8",
        errors="replace", timeout=timeout, check=False,
    )
    if check and cp.returncode:
        raise RuntimeError(f"docker command failed rc={cp.returncode}")
    return cp


def load_webhook_token() -> str:
    path = runtime_dir() / "runtime.env"
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw and not raw.lstrip().startswith("#") and "=" in raw:
            key, value = raw.split("=", 1)
            values[key.strip()] = value.strip()
    token = values.get("GITLAB_WEBHOOK_TOKEN", "")
    if not token:
        raise RuntimeError("GITLAB_WEBHOOK_TOKEN missing")
    return token


def request_json(url: str, *, token: str | None = None, event_uuid: str | None = None,
                 payload: dict | None = None) -> tuple[int, dict]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers.update({"Content-Type": "application/json", "X-Gitlab-Event": "Push Hook"})
        if event_uuid:
            headers["X-Gitlab-Event-UUID"] = event_uuid
        if token:
            headers["X-Gitlab-Token"] = token
    req = Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urlopen(req, timeout=12) as response:
            raw = response.read(262144)
            return int(response.status), json.loads(raw.decode("utf-8")) if raw else {}
    except HTTPError as exc:
        raw = exc.read(262144)
        return int(exc.code), json.loads(raw.decode("utf-8")) if raw else {}


def wait_tunnel_url(container: str, seconds: int = 60) -> str:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        logs = run("logs", container, check=False).stdout + run("logs", container, check=False).stderr
        match = TUNNEL_RE.search(logs)
        if match:
            return match.group(0).rstrip("/")
        inspect = run("inspect", "-f", "{{.State.Running}}", container, check=False)
        if inspect.returncode or inspect.stdout.strip().lower() != "true":
            raise RuntimeError("quick tunnel container stopped before publishing URL")
        time.sleep(2)
    raise RuntimeError("quick tunnel URL not published within timeout")


def wait_public_health(base_url: str, seconds: int = 60) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            status, payload = request_json(f"{base_url}/healthz")
            if status == 200 and payload.get("status") == "ok":
                return True
        except (URLError, OSError, TimeoutError, json.JSONDecodeError):
            pass
        time.sleep(2)
    return False


def sql_event(event_id: str) -> bool:
    safe = event_id.replace("'", "''")
    sql = f"SELECT count(*) FROM todo_bus.queue_events WHERE event_id = '{safe}'"
    cp = run("exec", DB_CONTAINER, "psql", "-U", DB_USER, "-d", DB_NAME, "-At", "-c", sql)
    return cp.stdout.strip() == "1"


def main() -> int:
    token = load_webhook_token()
    suffix = uuid.uuid4().hex[:12]
    container = f"todo-global-quick-tunnel-{suffix}"
    tunnel_url = ""
    cleanup_ok = False
    evidence: dict[str, object] = {
        "contract": "todo-global-cloudflare-quick-tunnel-e2e",
        "generated_at": datetime.now(UTC).isoformat(),
        "cloudflared_image": CLOUDFLARED_IMAGE,
        "temporary": True,
    }
    try:
        run(
            "run", "-d", "--name", container, "--network", DOCKER_NETWORK,
            CLOUDFLARED_IMAGE, "tunnel", "--no-autoupdate", "--url", GATEWAY_ORIGIN,
            timeout=120,
        )
        tunnel_url = wait_tunnel_url(container)
        public_health = wait_public_health(tunnel_url)
        event_uuid = str(uuid.uuid4())
        payload = {
            "object_kind": "push",
            "ref": "refs/heads/main",
            "after": uuid.uuid4().hex,
            "project": {"id": 84366761, "path_with_namespace": PROJECT},
        }
        denied, _ = request_json(f"{tunnel_url}/v1/webhooks/gitlab", event_uuid=event_uuid, payload=payload)
        first_status, first = request_json(
            f"{tunnel_url}/v1/webhooks/gitlab", token=token, event_uuid=event_uuid, payload=payload
        )
        replay_status, replay = request_json(
            f"{tunnel_url}/v1/webhooks/gitlab", token=token, event_uuid=event_uuid, payload=payload
        )
        event_id = str(first.get("event_id") or "")
        persisted = bool(event_id) and sql_event(event_id)
        ready = all([
            public_health,
            denied == 401,
            first_status == 202 and first.get("duplicate") is False,
            replay_status == 202 and replay.get("duplicate") is True,
            persisted,
        ])
        evidence.update({
            "public_url": tunnel_url,
            "public_health": public_health,
            "unauthorized_rejected": denied == 401,
            "accepted": first_status == 202 and first.get("duplicate") is False,
            "replay_idempotent": replay_status == 202 and replay.get("duplicate") is True,
            "sql_readback": persisted,
            "event_id": event_id,
            "ready": ready,
        })
        return_code = 0 if ready else 1
    finally:
        if tunnel_url:
            evidence["public_url"] = tunnel_url
        cleanup = run("rm", "-f", container, check=False, timeout=30)
        cleanup_ok = cleanup.returncode == 0
        evidence["cleanup_ok"] = cleanup_ok
        evidence["public_route_active_after_test"] = False
        out = runtime_dir() / "evidence" / "cloudflare-quick-tunnel-e2e-last.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(evidence, ensure_ascii=True))
    return return_code if cleanup_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

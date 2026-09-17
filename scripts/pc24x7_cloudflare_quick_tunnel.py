#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
import time

NAME = "todo-global-24x7-cloudflared"
NETWORK = "todo-global-24x7_default"
IMAGE = "cloudflare/cloudflared:latest"
TARGET = "http://gateway:8000"
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def docker(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def ensure_container() -> None:
    inspect = docker("inspect", NAME, timeout=30)
    if inspect.returncode == 0:
        started = docker("start", NAME, timeout=30)
        if started.returncode != 0:
            raise RuntimeError("falha ao iniciar túnel existente")
        return
    created = docker(
        "run", "-d", "--name", NAME,
        "--restart", "unless-stopped",
        "--network", NETWORK,
        IMAGE,
        "tunnel", "--no-autoupdate", "--url", TARGET,
        timeout=180,
    )
    if created.returncode != 0:
        raise RuntimeError("falha ao criar túnel Cloudflare")


def public_url(wait_seconds: int = 45) -> str:
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        logs = docker("logs", NAME, timeout=30)
        match = URL_RE.search(logs.stdout + "\n" + logs.stderr)
        if match:
            return match.group(0)
        time.sleep(2)
    raise RuntimeError("URL pública do túnel não encontrada")


def main() -> int:
    ensure_container()
    url = public_url()
    print(json.dumps({
        "result": "QUICK_TUNNEL_READY",
        "container": NAME,
        "public_url": url,
        "target": TARGET,
        "durability": "ephemeral_url",
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

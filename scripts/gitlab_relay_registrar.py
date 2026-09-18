from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
MUTEX_NAME = "Global\\ReqSysGitLabRelayRegistrar"


def runtime_dir() -> Path:
    return Path(os.environ["LOCALAPPDATA"]) / "ReqSys" / "TodoGlobal24x7"


def acquire_mutex() -> object:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        raise RuntimeError("cannot create registrar mutex")
    if kernel32.GetLastError() == 183:
        raise SystemExit(0)
    return handle


def current_tunnel_url() -> str:
    proc = subprocess.run(
        ["docker", "logs", "--tail", "250", "todo-global-24x7-cloudflared"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    urls = URL_RE.findall(text)
    if proc.returncode != 0 or not urls:
        raise RuntimeError("current quick tunnel URL not found")
    return urls[-1].rstrip("/")


def load_config() -> tuple[str, str]:
    base = runtime_dir()
    relay_url = (base / "gitlab-relay-url.txt").read_text(encoding="utf-8").strip().rstrip("/")
    token = (base / "gitlab-relay-admin.token").read_text(encoding="utf-8").strip()
    if not relay_url.startswith("https://") or len(token) < 20:
        raise RuntimeError("relay configuration invalid")
    return relay_url, token


def register_once() -> dict[str, object]:
    relay_url, token = load_config()
    target = current_tunnel_url()
    payload = json.dumps({"base_url": target}).encode("utf-8")
    request = Request(
        relay_url + "/admin/target",
        data=payload,
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            body = json.loads(response.read(4096).decode("utf-8"))
            if response.status != 200 or body.get("status") != "updated":
                raise RuntimeError("relay registration rejected")
    except HTTPError as exc:
        raise RuntimeError(f"relay registration http={exc.code}") from exc
    except (URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"relay registration failed: {type(exc).__name__}") from exc
    return {"status": "registered", "target": target}


def main() -> int:
    acquire_mutex()
    once = "--once" in os.sys.argv
    while True:
        try:
            print(json.dumps(register_once(), sort_keys=True), flush=True)
        except Exception as exc:
            print(json.dumps({"status": "retry", "error_type": type(exc).__name__}), flush=True)
        if once:
            return 0
        time.sleep(60)


if __name__ == "__main__":
    raise SystemExit(main())

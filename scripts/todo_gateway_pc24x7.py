#!/usr/bin/env python3
"""Bootstrap governado do TODO Gateway no host PC24x7, sem expor segredos."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PORT = 8094
POSTGRES_PORT = 55432
COMPOSE = "docker-compose.pc24x7.yml"
REDACT_PATTERNS = (
    (re.compile(r"postgres(?:ql)?://[^@\s]+@", re.I), "postgresql://[REDACTED]@"),
    (re.compile(r"(?i)(password|token|secret|api[_-]?key)=([^\s]+)"), r"\1=[REDACTED]"),
)


def runtime_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "ReqSys" / "TodoGlobal24x7"
    return Path.home() / ".config" / "todo-global-24x7"


def redact(text: str) -> str:
    for pattern, replacement in REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def windows_child_env() -> dict[str, str]:
    env = os.environ.copy()
    if os.name == "nt":
        program_data = (
            env.get("ProgramData")
            or env.get("PROGRAMDATA")
            or env.get("ALLUSERSPROFILE")
            or r"C:\ProgramData"
        )
        env["ProgramData"] = program_data
        env["PROGRAMDATA"] = program_data
    return env


def docker_ready() -> bool:
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            text=True,
            capture_output=True,
            timeout=12,
            check=False,
            env=windows_child_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def docker_desktop_candidates() -> list[Path]:
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    return [
        program_files / "Docker" / "Docker" / "Docker Desktop.exe",
        local / "Programs" / "DockerDesktop" / "Docker Desktop.exe",
        local / "Docker" / "Docker Desktop.exe",
    ]


def start_docker_desktop() -> bool:
    if os.name != "nt":
        return False
    target = next((item for item in docker_desktop_candidates() if item.is_file()), None)
    if target is None:
        return False
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen(
            [str(target)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
            env=windows_child_env(),
        )
    except OSError:
        return False
    return True


def ensure_docker_ready(wait_seconds: int = 120) -> bool:
    if docker_ready():
        return True
    if not start_docker_desktop():
        return False
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if docker_ready():
            return True
        time.sleep(3)
    return False


def ensure_port_setting(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    updated: list[str] = []
    gateway_found = False
    postgres_found = False
    for line in lines:
        if line.startswith("TODO_GATEWAY_PORT="):
            updated.append(f"TODO_GATEWAY_PORT={PORT}")
            gateway_found = True
        elif line.startswith("TODO_POSTGRES_PORT="):
            updated.append(f"TODO_POSTGRES_PORT={POSTGRES_PORT}")
            postgres_found = True
        else:
            updated.append(line)
    if not gateway_found:
        updated.append(f"TODO_GATEWAY_PORT={PORT}")
    if not postgres_found:
        updated.append(f"TODO_POSTGRES_PORT={POSTGRES_PORT}")
    path.write_text("\n".join(updated) + "\n", encoding="utf-8", newline="\n")


def ensure_runtime_env() -> Path:
    root = runtime_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / "runtime.env"
    if path.exists():
        ensure_port_setting(path)
        return path
    password = secrets.token_urlsafe(32)
    token = secrets.token_urlsafe(40)
    content = "\n".join(
        [
            "POSTGRES_DB=todo_global_bus_dev",
            "POSTGRES_USER=todo_global_bus_dev_user",
            f"POSTGRES_PASSWORD={password}",
            f"TODO_GATEWAY_TOKEN={token}",
            f"DATABASE_URL=postgresql://todo_global_bus_dev_user:{password}@db:5432/todo_global_bus_dev",
            f"TODO_GATEWAY_PORT={PORT}",
            f"TODO_POSTGRES_PORT={POSTGRES_PORT}",
            "",
        ]
    )
    path.write_text(content, encoding="utf-8", newline="\n")
    if os.name != "nt":
        path.chmod(0o600)
    return path


def compose(repo_root: Path, env_file: Path, *args: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    cmd = ["docker", "compose", "--env-file", str(env_file), "-f", COMPOSE, *args]
    return subprocess.run(
        cmd,
        cwd=repo_root,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        env=windows_child_env(),
    )


def probe(path: str, timeout: float = 4.0) -> dict[str, object]:
    url = f"http://127.0.0.1:{PORT}{path}"
    started = time.perf_counter()
    try:
        with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=timeout) as response:
            raw = response.read(131072)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                payload = None
            return {
                "path": path,
                "ok": 200 <= response.status < 300,
                "status_code": response.status,
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
                "payload": payload if isinstance(payload, dict) else None,
            }
    except (HTTPError, URLError, OSError, TimeoutError) as exc:
        return {
            "path": path,
            "ok": False,
            "status_code": getattr(exc, "code", None),
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "error": redact(f"{type(exc).__name__}: {exc}"),
        }


def smoke(wait_seconds: int) -> dict[str, object]:
    deadline = time.monotonic() + wait_seconds
    results: list[dict[str, object]] = []
    while time.monotonic() < deadline:
        results = [probe("/healthz"), probe("/readyz")]
        if all(bool(item.get("ok")) for item in results):
            return {"ready": True, "port": PORT, "results": results}
        time.sleep(2)
    return {"ready": False, "port": PORT, "results": results}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("up", "status", "smoke"), nargs="?", default="up")
    parser.add_argument("--wait-seconds", type=int, default=90)
    parser.add_argument("--docker-wait-seconds", type=int, default=120)
    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    env_file = ensure_runtime_env()

    if args.action == "up":
        if not ensure_docker_ready(args.docker_wait_seconds):
            print(json.dumps({"action": "up", "ready": False, "docker_ready": False}))
            return 2
        result = compose(repo_root, env_file, "up", "-d", "--build")
        if result.returncode != 0:
            print(json.dumps({"action": "up", "ready": False, "docker_rc": result.returncode, "stderr": redact(result.stderr[-4000:])}))
            return result.returncode or 1
        evidence = smoke(args.wait_seconds)
        print(json.dumps({"action": "up", "docker_ready": True, **evidence}, ensure_ascii=True))
        return 0 if evidence["ready"] else 1

    if args.action == "status":
        if not docker_ready():
            print(json.dumps({"action": "status", "docker_ready": False}))
            return 2
        result = compose(repo_root, env_file, "ps", "--format", "json", timeout=60)
        print(json.dumps({"action": "status", "docker_ready": True, "docker_rc": result.returncode, "output": redact(result.stdout[-8000:]), "stderr": redact(result.stderr[-2000:])}, ensure_ascii=True))
        return result.returncode

    evidence = smoke(args.wait_seconds)
    print(json.dumps({"action": "smoke", **evidence}, ensure_ascii=True))
    return 0 if evidence["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

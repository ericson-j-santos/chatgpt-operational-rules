from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Mapping, Sequence

LOGGER = logging.getLogger("todo_gateway_service")
ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATHS = (
    ROOT / "sql" / "todo_bus_postgres.sql",
    ROOT / "sql" / "todo_control_plane_postgres.sql",
)
REQUIRED_ENV = (
    "DATABASE_URL",
    "TODO_GATEWAY_TOKEN",
    "NOTION_TOKEN",
    "NOTION_DATA_SOURCE_ID",
)


def validate_environment(env: Mapping[str, str]) -> None:
    missing = [key for key in REQUIRED_ENV if not str(env.get(key, "")).strip()]
    if missing:
        raise RuntimeError("required environment is missing: " + ", ".join(missing))


def bootstrap_schema(
    database_url: str,
    schema_path: Path | Sequence[Path] = SCHEMA_PATHS,
) -> None:
    import psycopg

    paths = (schema_path,) if isinstance(schema_path, Path) else tuple(schema_path)
    with psycopg.connect(database_url) as conn:
        for path in paths:
            conn.execute(path.read_text(encoding="utf-8"))


def _terminate(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()

    deadline = time.monotonic() + 10.0
    for process in processes:
        remaining = max(0.0, deadline - time.monotonic())
        if process.poll() is None:
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                process.kill()

    for process in processes:
        if process.poll() is None:
            process.wait(timeout=5.0)


def run_supervised(
    env: Mapping[str, str] | None = None,
    *,
    popen=subprocess.Popen,
    sleep=time.sleep,
) -> int:
    effective_env = dict(os.environ if env is None else env)
    validate_environment(effective_env)
    bootstrap_schema(effective_env["DATABASE_URL"])

    port = str(effective_env.get("PORT", "8000"))
    poll_seconds = str(effective_env.get("TODO_WORKER_POLL_SECONDS", "2"))

    worker_cmd = [
        sys.executable,
        "-m",
        "services.todo_gateway.worker",
        "--poll-seconds",
        poll_seconds,
    ]
    web_cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "services.todo_gateway.asgi:app",
        "--host",
        "0.0.0.0",
        "--port",
        port,
    ]

    worker = popen(worker_cmd, env=effective_env)
    web = popen(web_cmd, env=effective_env)
    processes = [worker, web]

    try:
        while True:
            for name, process in (("worker", worker), ("web", web)):
                return_code = process.poll()
                if return_code is not None:
                    LOGGER.error("%s exited unexpectedly rc=%s", name, return_code)
                    return return_code if return_code != 0 else 1
            sleep(0.5)
    finally:
        _terminate(processes)


def _install_signal_handlers() -> None:
    def _stop(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    _install_signal_handlers()
    return run_supervised()


if __name__ == "__main__":
    raise SystemExit(main())

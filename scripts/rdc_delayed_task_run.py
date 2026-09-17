#!/usr/bin/env python3
"""Schedule one delayed run of the governed Remote Desktop Commander task."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ALLOWED_TASK = r"\Automation\RemoteDesktopCommander"
RECEIPT = Path(r"C:\RemoteDesktopCommander\rdc-restart-receipt.json")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_receipt(payload: dict[str, object]) -> None:
    temp = RECEIPT.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, RECEIPT)


def worker(task_name: str, delay: int) -> int:
    time.sleep(delay)
    completed = subprocess.run(
        ["schtasks.exe", "/Run", "/TN", task_name],
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        timeout=30,
        check=False,
    )
    write_receipt(
        {
            "timestamp": utc_now(),
            "task_name": task_name,
            "delay_seconds": delay,
            "exit_code": completed.returncode,
            "result": "triggered" if completed.returncode == 0 else "failed",
        }
    )
    return completed.returncode


def schedule(task_name: str, delay: int) -> dict[str, object]:
    if task_name != ALLOWED_TASK:
        raise ValueError("task name not authorized")
    if not 5 <= delay <= 120:
        raise ValueError("delay must be between 5 and 120 seconds")
    args = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--task-name",
        task_name,
        "--delay",
        str(delay),
    ]
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_PROCESS_GROUP | NO_WINDOW
    process = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=flags,
    )
    return {"result": "scheduled", "worker_pid": process.pid, "delay_seconds": delay, "task_name": task_name}


def main() -> int:
    parser = argparse.ArgumentParser(description="Delayed RDC task trigger")
    parser.add_argument("--task-name", default=ALLOWED_TASK)
    parser.add_argument("--delay", type=int, default=20)
    parser.add_argument("--worker", action="store_true")
    ns = parser.parse_args()
    try:
        if ns.task_name != ALLOWED_TASK:
            raise ValueError("task name not authorized")
        if ns.worker:
            return worker(ns.task_name, ns.delay)
        print(json.dumps(schedule(ns.task_name, ns.delay), ensure_ascii=True, sort_keys=True))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"result": "blocked", "error": str(exc)}, ensure_ascii=True, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

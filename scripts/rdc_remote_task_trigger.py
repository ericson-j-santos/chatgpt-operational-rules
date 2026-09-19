#!/usr/bin/env python3
"""Inspect or trigger the existing RDC scheduled task on one remote Windows host.

This script never creates, edits, enables, disables, or deletes a task.
It only opens the pre-existing \Automation\RemoteDesktopCommander task and,
when explicitly confirmed, requests one run.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
from typing import Any

TASK_FOLDER = r"\Automation"
TASK_NAME = "RemoteDesktopCommander"
RUN_CONFIRM = "RUN-EXISTING-RDC-TASK"
TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$")


class TriggerError(RuntimeError):
    pass


def validate_target(value: str) -> str:
    target = value.strip()
    if not TARGET_RE.fullmatch(target):
        raise TriggerError("target inválido")
    if target.casefold() == socket.gethostname().casefold():
        raise TriggerError("target deve ser remoto")
    return target


def connect(target: str):
    try:
        import win32com.client
    except ImportError as exc:
        raise TriggerError("pywin32 indisponível") from exc
    svc = win32com.client.Dispatch("Schedule.Service")
    svc.Connect(target)
    folder = svc.GetFolder(TASK_FOLDER)
    return folder


def snapshot(task) -> dict[str, Any]:
    return {
        "path": str(task.Path),
        "enabled": bool(task.Enabled),
        "state": int(task.State),
        "last_task_result": int(task.LastTaskResult),
        "task_name": TASK_NAME,
        "task_folder": TASK_FOLDER,
    }


def inspect(target: str) -> dict[str, Any]:
    folder = connect(validate_target(target))
    task = folder.GetTask(TASK_NAME)
    state = snapshot(task)
    return {"ok": True, "action": "inspect", "target": target, "task": state}


def run_existing(target: str, confirm: str) -> dict[str, Any]:
    if confirm != RUN_CONFIRM:
        raise TriggerError("confirmação inválida")
    target = validate_target(target)
    folder = connect(target)
    task = folder.GetTask(TASK_NAME)
    before = snapshot(task)
    if not before["enabled"]:
        raise TriggerError("tarefa existente está desabilitada")
    running = task.Run("")
    after = snapshot(task)
    return {
        "ok": True,
        "action": "run_existing",
        "target": target,
        "task": after,
        "before": before,
        "running_instance": str(getattr(running, "InstanceGuid", "")),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--run-existing", action="store_true")
    parser.add_argument("--confirm")
    args = parser.parse_args()
    try:
        result = (
            run_existing(args.target, args.confirm or "")
            if args.run_existing
            else inspect(args.target)
        )
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc), "error_type": type(exc).__name__},
                ensure_ascii=True,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

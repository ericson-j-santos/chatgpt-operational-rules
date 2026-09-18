#!/usr/bin/env python3
"""Inspect the governed Remote Desktop Commander scheduled task without reading secrets."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import xml.etree.ElementTree as ET

ALLOWED_TASK = r"\Automation\RemoteDesktopCommander"


def run(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        timeout=timeout,
        check=False,
    )


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def text_of(parent: ET.Element, name: str) -> str | None:
    for node in parent.iter():
        if local_name(node.tag) == name:
            value = (node.text or "").strip()
            return value or None
    return None


def inspect(task_name: str) -> dict[str, object]:
    if task_name != ALLOWED_TASK:
        raise ValueError("task name not authorized")
    completed = run(["schtasks.exe", "/Query", "/TN", task_name, "/XML"])
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "task query failed")

    root = ET.fromstring(completed.stdout)
    triggers: list[dict[str, object]] = []
    for node in root.iter():
        name = local_name(node.tag)
        if name not in {"BootTrigger", "LogonTrigger", "RegistrationTrigger", "TimeTrigger"}:
            continue
        triggers.append(
            {
                "type": name,
                "enabled": text_of(node, "Enabled"),
                "delay": text_of(node, "Delay"),
                "start_boundary": text_of(node, "StartBoundary"),
                "user_id": text_of(node, "UserId"),
            }
        )

    principal = None
    for node in root.iter():
        if local_name(node.tag) == "Principal":
            principal = {
                "user_id": text_of(node, "UserId"),
                "group_id": text_of(node, "GroupId"),
                "logon_type": text_of(node, "LogonType"),
                "run_level": text_of(node, "RunLevel"),
            }
            break

    settings = {}
    for key in [
        "MultipleInstancesPolicy",
        "DisallowStartIfOnBatteries",
        "StopIfGoingOnBatteries",
        "StartWhenAvailable",
        "ExecutionTimeLimit",
        "Enabled",
        "WakeToRun",
    ]:
        settings[key] = text_of(root, key)

    actions = []
    for node in root.iter():
        if local_name(node.tag) == "Exec":
            actions.append(
                {
                    "command": text_of(node, "Command"),
                    "arguments": text_of(node, "Arguments"),
                    "working_directory": text_of(node, "WorkingDirectory"),
                }
            )

    has_boot_trigger = any(item["type"] == "BootTrigger" for item in triggers)
    logon_type = (principal or {}).get("logon_type")
    headless_candidate = has_boot_trigger and logon_type not in {"InteractiveToken", None, ""}

    return {
        "host": os.environ.get("COMPUTERNAME", ""),
        "task_name": task_name,
        "principal": principal,
        "triggers": triggers,
        "settings": settings,
        "actions": actions,
        "has_boot_trigger": has_boot_trigger,
        "headless_candidate": headless_candidate,
        "secret_content_read": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect RDC scheduled task headless readiness")
    parser.add_argument("--task-name", default=ALLOWED_TASK)
    ns = parser.parse_args()
    try:
        print(json.dumps(inspect(ns.task_name), ensure_ascii=True, sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError, ET.ParseError, subprocess.SubprocessError) as exc:
        print(json.dumps({"result": "blocked", "error": str(exc)}, ensure_ascii=True, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

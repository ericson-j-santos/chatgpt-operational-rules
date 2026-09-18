#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

TASK_FOLDER = r"\Automation"
TASK_NAME = "RemoteDesktopCommander"
LAUNCHER = Path(r"C:\RemoteDesktopCommander\start-remote-desktop-commander.cmd")
TASK_CREATE_OR_UPDATE = 6
TASK_LOGON_INTERACTIVE_TOKEN = 3
TASK_RUNLEVEL_LUA = 0
TASK_TRIGGER_LOGON = 9
TASK_TRIGGER_DAILY = 2
TASK_ACTION_EXEC = 0
TASK_INSTANCES_IGNORE_NEW = 2


def identity() -> str:
    domain = os.environ.get("USERDOMAIN", "").strip()
    user = os.environ.get("USERNAME", "").strip() or getpass.getuser()
    return f"{domain}\\{user}" if domain else user


def connect():
    import win32com.client
    svc = win32com.client.Dispatch("Schedule.Service")
    svc.Connect()
    root = svc.GetFolder("\\")
    try:
        folder = svc.GetFolder(TASK_FOLDER)
    except Exception:
        folder = root.CreateFolder("Automation")
    return svc, folder


def configure(definition, *, start_in_seconds: int) -> None:
    definition.RegistrationInfo.Description = (
        "Governed resilient Remote Desktop Commander supervisor"
    )
    settings = definition.Settings
    settings.Enabled = True
    settings.StartWhenAvailable = True
    settings.DisallowStartIfOnBatteries = False
    settings.StopIfGoingOnBatteries = False
    settings.ExecutionTimeLimit = "PT0S"
    settings.MultipleInstances = TASK_INSTANCES_IGNORE_NEW
    settings.RestartCount = 999
    settings.RestartInterval = "PT1M"
    try:
        settings.RunOnlyIfNetworkAvailable = False
    except Exception:
        pass

    principal = definition.Principal
    principal.UserId = identity()
    principal.LogonType = TASK_LOGON_INTERACTIVE_TOKEN
    principal.RunLevel = TASK_RUNLEVEL_LUA

    triggers = definition.Triggers
    logon = triggers.Create(TASK_TRIGGER_LOGON)
    logon.Enabled = True
    logon.UserId = identity()

    daily = triggers.Create(TASK_TRIGGER_DAILY)
    daily.Enabled = True
    daily.StartBoundary = (
        datetime.now().astimezone() + timedelta(seconds=start_in_seconds)
    ).replace(microsecond=0).isoformat()
    daily.DaysInterval = 1
    daily.Repetition.Interval = "PT5M"
    daily.Repetition.Duration = "P1D"
    daily.Repetition.StopAtDurationEnd = False

    action = definition.Actions.Create(TASK_ACTION_EXEC)
    action.Path = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
    action.Arguments = f'/d /c ""{LAUNCHER}""'


def snapshot(task) -> dict[str, object]:
    definition = task.Definition
    settings = definition.Settings
    return {
        "path": task.Path,
        "enabled": bool(settings.Enabled),
        "start_when_available": bool(settings.StartWhenAvailable),
        "disallow_start_on_battery": bool(settings.DisallowStartIfOnBatteries),
        "stop_on_battery": bool(settings.StopIfGoingOnBatteries),
        "execution_time_limit": str(settings.ExecutionTimeLimit),
        "run_only_if_network_available": bool(getattr(settings, "RunOnlyIfNetworkAvailable", False)),
        "multiple_instances": int(settings.MultipleInstances),
        "restart_count": int(settings.RestartCount),
        "restart_interval": str(settings.RestartInterval),
        "principal_logon_type": int(definition.Principal.LogonType),
        "principal_run_level": int(definition.Principal.RunLevel),
        "trigger_count": int(definition.Triggers.Count),
        "action_count": int(definition.Actions.Count),
    }


def apply(start_in_seconds: int) -> dict[str, object]:
    if not LAUNCHER.is_file():
        raise FileNotFoundError(LAUNCHER)
    svc, folder = connect()
    definition = svc.NewTask(0)
    configure(definition, start_in_seconds=start_in_seconds)
    task = folder.RegisterTaskDefinition(
        TASK_NAME,
        definition,
        TASK_CREATE_OR_UPDATE,
        identity(),
        None,
        TASK_LOGON_INTERACTIVE_TOKEN,
    )
    state = snapshot(task)
    expected = {
        "enabled": True,
        "start_when_available": True,
        "disallow_start_on_battery": False,
        "stop_on_battery": False,
        "execution_time_limit": "PT0S",
        "run_only_if_network_available": False,
        "multiple_instances": TASK_INSTANCES_IGNORE_NEW,
        "restart_count": 999,
        "restart_interval": "PT1M",
        "principal_logon_type": TASK_LOGON_INTERACTIVE_TOKEN,
        "principal_run_level": TASK_RUNLEVEL_LUA,
        "trigger_count": 2,
        "action_count": 1,
    }
    mismatches = {
        key: {"expected": value, "actual": state.get(key)}
        for key, value in expected.items()
        if state.get(key) != value
    }
    if mismatches:
        raise RuntimeError("task validation failed: " + json.dumps(mismatches, sort_keys=True))
    return {"result": "TASK_RESILIENCE_APPLIED", **state}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--start-in-seconds", type=int, default=30)
    args = parser.parse_args()
    try:
        if not 20 <= args.start_in_seconds <= 120:
            raise ValueError("start-in-seconds must be 20..120")
        if not args.apply:
            print(json.dumps({"result": "dry_run", "identity": identity(), "launcher_exists": LAUNCHER.is_file()}, sort_keys=True))
            return 0
        print(json.dumps(apply(args.start_in_seconds), sort_keys=True))
        return 0
    except Exception as exc:
        payload = {"result": "blocked", "error": str(exc), "error_type": type(exc).__name__}
        try:
            error_path = Path(r"C:\RemoteDesktopCommander\rdc-task-resilience-error.json")
            error_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        except OSError:
            pass
        print(json.dumps(payload, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
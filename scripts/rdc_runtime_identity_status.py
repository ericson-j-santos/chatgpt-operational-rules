from __future__ import annotations

import json
import os
import shutil
import socket

import win32com.client  # type: ignore

TASK_FOLDER = r"\Automation"
TASKS = ("RemoteDesktopCommander", "RemoteDesktopCommanderHeadless")


def task_info(folder, name: str) -> dict[str, object]:
    try:
        task = folder.GetTask(name)
        d = task.Definition
        return {
            "present": True,
            "enabled": bool(task.Enabled),
            "state": int(task.State),
            "last_result": int(task.LastTaskResult),
            "principal_user": str(d.Principal.UserId or ""),
            "principal_logon_type": int(d.Principal.LogonType),
            "principal_run_level": int(d.Principal.RunLevel),
            "triggers": [int(t.Type) for t in d.Triggers],
            "actions": [
                {
                    "type": int(a.Type),
                    "path": str(getattr(a, "Path", "") or ""),
                    "arguments": str(getattr(a, "Arguments", "") or ""),
                    "working_directory": str(getattr(a, "WorkingDirectory", "") or ""),
                }
                for a in d.Actions
            ],
        }
    except Exception as exc:
        return {"present": False, "error_type": type(exc).__name__}


def main() -> int:
    result: dict[str, object] = {
        "host": socket.gethostname(),
        "current_user": os.environ.get("USERNAME", ""),
        "user_profile": os.environ.get("USERPROFILE", ""),
        "python_on_path": shutil.which("python") or "",
        "git_on_path": shutil.which("git") or "",
        "node_on_path": shutil.which("node") or "",
        "secret_value_exposed": False,
    }
    try:
        service = win32com.client.Dispatch("Schedule.Service")
        service.Connect()
        folder = service.GetFolder(TASK_FOLDER)
        result["tasks"] = {name: task_info(folder, name) for name in TASKS}
    except Exception as exc:
        result["task_scheduler_error_type"] = type(exc).__name__
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

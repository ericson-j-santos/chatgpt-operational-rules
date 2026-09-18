from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import win32com.client  # type: ignore

LOG = Path(r"C:\ProgramData\ReqSys\RdcSvc\rdc-headless.log")
TASK_FOLDER = r"\Automation"
TASK = "RemoteDesktopCommanderHeadless"


def main() -> int:
    result: dict[str, object] = {"secret_value_exposed": False}
    try:
        wmi = win32com.client.GetObject("winmgmts:")
        rows = list(wmi.ExecQuery("SELECT LastBootUpTime,LocalDateTime FROM Win32_OperatingSystem"))
        if rows:
            osrow = rows[0]
            result["last_boot_up_time_raw"] = str(osrow.LastBootUpTime or "")
            result["local_date_time_raw"] = str(osrow.LocalDateTime or "")
    except Exception as exc:
        result["wmi_error_type"] = type(exc).__name__

    if LOG.is_file():
        text = LOG.read_text(encoding="utf-8", errors="replace")
        result["log_mtime_utc"] = datetime.utcfromtimestamp(LOG.stat().st_mtime).isoformat() + "Z"
        result["runner_start_count"] = text.count("runner_start")
        result["child_exit_count"] = text.count("child_exit")
        result["device_ready_count"] = text.count("Device ready:")
        result["session_restored_count"] = text.count("Session restored")

    try:
        service = win32com.client.Dispatch("Schedule.Service")
        service.Connect()
        folder = service.GetFolder(TASK_FOLDER)
        task = folder.GetTask(TASK)
        result["task_last_run_time"] = str(task.LastRunTime)
        result["task_last_result"] = int(task.LastTaskResult)
        result["task_state"] = int(task.State)
        result["task_enabled"] = bool(task.Enabled)
    except Exception as exc:
        result["task_error_type"] = type(exc).__name__

    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

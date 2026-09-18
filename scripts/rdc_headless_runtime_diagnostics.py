from __future__ import annotations

import json
from pathlib import Path

import win32com.client  # type: ignore

RUNTIME = Path(r"C:\ProgramData\ReqSys\RdcSvc")
LOG = RUNTIME / "rdc-headless.log"
TASK_FOLDER = r"\Automation"
TASK = "RemoteDesktopCommanderHeadless"


def main() -> int:
    result: dict[str, object] = {
        "log_present": LOG.is_file(),
        "secret_value_exposed": False,
    }
    if LOG.is_file():
        text = LOG.read_text(encoding="utf-8", errors="replace")
        result.update(
            {
                "log_size": LOG.stat().st_size,
                "runner_start_count": text.count("runner_start"),
                "session_restored_count": text.count("Session restored"),
                "device_ready_count": text.count("Device ready:"),
                "startup_failed_count": text.count("Device startup failed"),
                "persisted_invalid_count": text.count("Persisted session invalid"),
                "child_exit_count": text.count("child_exit"),
                "duplicate_device_count": text.casefold().count("duplicate"),
                "already_connected_count": text.casefold().count("already connected"),
                "auth_error_count": text.casefold().count("auth error"),
            }
        )

    try:
        service = win32com.client.Dispatch("Schedule.Service")
        service.Connect()
        folder = service.GetFolder(TASK_FOLDER)
        task = folder.GetTask(TASK)
        result.update(
            {
                "task_enabled": bool(task.Enabled),
                "task_state": int(task.State),
                "task_last_result": int(task.LastTaskResult),
                "task_last_run_time": str(task.LastRunTime),
                "task_next_run_time": str(task.NextRunTime),
                "task_missed_runs": int(task.NumberOfMissedRuns),
            }
        )
    except Exception as exc:
        result["task_error_type"] = type(exc).__name__

    try:
        wmi = win32com.client.GetObject("winmgmts:")
        rows = []
        for proc in wmi.ExecQuery("SELECT ProcessId,Name,CommandLine,ExecutablePath FROM Win32_Process WHERE Name='node.exe'"):
            owner = ""
            try:
                owner_result = proc.GetOwner()
                if isinstance(owner_result, tuple) and len(owner_result) >= 2 and int(owner_result[-1]) == 0:
                    owner = str(owner_result[0] or "")
            except Exception:
                pass
            command = str(proc.CommandLine or "")
            if ".desktop-commander-device" in command.casefold():
                command = "[redacted-sensitive-path]"
            rows.append(
                {
                    "pid": int(proc.ProcessId),
                    "owner": owner,
                    "executable_path": str(proc.ExecutablePath or ""),
                    "is_headless_runner": "rdc-headless-runner.cjs" in command,
                    "is_desktop_commander": "desktop-commander" in command.casefold(),
                }
            )
        result["node_processes"] = rows
    except Exception as exc:
        result["process_probe_error_type"] = type(exc).__name__

    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

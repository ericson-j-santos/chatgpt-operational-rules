from __future__ import annotations

import json
from pathlib import Path

import win32com.client  # type: ignore

RUNTIME = Path(r"C:\ProgramData\ReqSys\RdcSvc")
RECEIPT = RUNTIME / "noteri-provision-receipt.json"
TASK_FOLDER = r"\Automation"
TASK = "RemoteDesktopCommanderHeadless"


def main() -> int:
    result: dict[str, object] = {
        "receipt_present": RECEIPT.is_file(),
        "secret_value_exposed": False,
    }
    if RECEIPT.is_file():
        try:
            payload = json.loads(RECEIPT.read_text(encoding="utf-8"))
            for key in (
                "result",
                "stage",
                "correlation_id",
                "error_type",
                "account_created",
                "headless_task_present",
                "headless_task_enabled",
                "headless_task_state",
                "headless_task_last_result",
                "headless_trigger_boot",
                "headless_logon_type_password",
                "package_version",
                "service_python_present",
                "service_node_present",
                "source_session_readable_by_probe",
                "toolchain_probe_last_result",
                "rdc_headless_started_during_provision",
                "legacy_controller_changed",
            ):
                result[f"receipt_{key}"] = payload.get(key)
        except Exception as exc:
            result["receipt_read_error_type"] = type(exc).__name__

    try:
        service = win32com.client.Dispatch("Schedule.Service")
        service.Connect()
        folder = service.GetFolder(TASK_FOLDER)
        task = folder.GetTask(TASK)
        result["task_present"] = True
        result["task_enabled"] = bool(task.Enabled)
        result["task_state"] = int(task.State)
        result["task_last_result"] = int(task.LastTaskResult)
        result["task_trigger_boot"] = any(int(t.Type) == 8 for t in task.Definition.Triggers)
        result["task_logon_type"] = int(task.Definition.Principal.LogonType)
    except Exception as exc:
        result["task_present"] = False
        result["task_error_type"] = type(exc).__name__

    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

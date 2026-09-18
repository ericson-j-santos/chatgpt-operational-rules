from __future__ import annotations

import ctypes
import json
import os
import shutil

ACCOUNT = "ReqSysRdcSvc"


def main() -> int:
    result: dict[str, object] = {
        "host": os.environ.get("COMPUTERNAME", ""),
        "current_user": os.environ.get("USERNAME", ""),
        "is_admin": bool(ctypes.windll.shell32.IsUserAnAdmin()),
        "node_path_present": bool(shutil.which("node")),
        "npx_path_present": bool(shutil.which("npx")),
        "service_account_name": ACCOUNT,
        "service_account_exists": False,
        "task_scheduler_com_available": False,
        "pywin32_available": False,
        "secret_value_read": False,
    }

    try:
        import win32net  # type: ignore
        result["pywin32_available"] = True
        try:
            info = win32net.NetUserGetInfo(None, ACCOUNT, 1)
            result["service_account_exists"] = bool(info)
        except Exception as exc:
            code = getattr(exc, "winerror", None)
            if code not in (2221,):
                result["service_account_probe_error"] = type(exc).__name__
    except Exception:
        pass

    try:
        import win32com.client  # type: ignore
        result["pywin32_available"] = True
        service = win32com.client.Dispatch("Schedule.Service")
        service.Connect()
        result["task_scheduler_com_available"] = True
    except Exception as exc:
        result["task_scheduler_com_error"] = type(exc).__name__

    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

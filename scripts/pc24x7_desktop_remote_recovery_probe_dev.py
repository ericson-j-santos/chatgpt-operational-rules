from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

HOST = "192.168.1.45"
TASK = "TodoGlobal24x7Headless"
ADMIN_SHARE = Path(r"\\192.168.1.45\c$")


def run(args: list[str], timeout: int = 20) -> dict[str, object]:
    try:
        cp = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return {
            "rc": cp.returncode,
            "stdout_tail": cp.stdout[-1200:],
            "stderr_tail": cp.stderr[-1200:],
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    share = {
        "exists": False,
        "listable": False,
        "error": "",
    }
    try:
        share["exists"] = ADMIN_SHARE.exists()
    except Exception as exc:
        share["error"] = f"{type(exc).__name__}: {exc}"
    if share["exists"]:
        try:
            entries = os.listdir(ADMIN_SHARE)
            share["listable"] = True
            share["entry_sample"] = sorted(entries)[:20]
        except Exception as exc:
            share["error"] = f"{type(exc).__name__}: {exc}"

    task = run(["schtasks.exe", "/Query", "/S", HOST, "/TN", TASK, "/XML"])
    sessions = run(["quser.exe", f"/server:{HOST}"])
    wmic = run(["wmic.exe", f"/node:{HOST}", "os", "get", "Caption,LastBootUpTime", "/value"])

    print(json.dumps({
        "host": HOST,
        "admin_share": share,
        "scheduled_task": task,
        "sessions": sessions,
        "wmi": wmic,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

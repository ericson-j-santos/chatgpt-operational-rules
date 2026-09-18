from __future__ import annotations

import json

import win32com.client  # type: ignore

KEYWORDS = ("automation", "reqsys", "remote", "desktop", "commander", "pc24x7", "gateway")


def trigger_type_name(value: int) -> str:
    return {
        0: "Event",
        1: "Time",
        2: "Daily",
        3: "Weekly",
        4: "Monthly",
        5: "MonthlyDOW",
        6: "Idle",
        7: "Registration",
        8: "Boot",
        9: "Logon",
        11: "SessionStateChange",
    }.get(int(value), str(value))


def collect_folder(folder, rows: list[dict[str, object]]) -> None:
    for task in folder.GetTasks(1):
        path = str(task.Path)
        definition = task.Definition
        principal = definition.Principal
        actions = []
        for action in definition.Actions:
            actions.append({
                "type": int(action.Type),
                "path": str(getattr(action, "Path", "") or ""),
                "arguments": str(getattr(action, "Arguments", "") or ""),
                "working_directory": str(getattr(action, "WorkingDirectory", "") or ""),
            })
        haystack = " ".join(
            [path, str(getattr(principal, "UserId", "") or "")]
            + [str(a.get("path", "")) + " " + str(a.get("arguments", "")) for a in actions]
        ).casefold()
        if any(k in haystack for k in KEYWORDS):
            triggers = [trigger_type_name(t.Type) for t in definition.Triggers]
            rows.append({
                "path": path,
                "enabled": bool(task.Enabled),
                "state": int(task.State),
                "principal_user": str(getattr(principal, "UserId", "") or ""),
                "principal_logon_type": int(getattr(principal, "LogonType", 0) or 0),
                "principal_run_level": int(getattr(principal, "RunLevel", 0) or 0),
                "triggers": triggers,
                "actions": actions,
            })
    for sub in folder.GetFolders(0):
        collect_folder(sub, rows)


def main() -> int:
    service = win32com.client.Dispatch("Schedule.Service")
    service.Connect()
    rows: list[dict[str, object]] = []
    collect_folder(service.GetFolder("\\"), rows)
    print(json.dumps({
        "matching_tasks": rows,
        "count": len(rows),
        "secret_value_exposed": False,
    }, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

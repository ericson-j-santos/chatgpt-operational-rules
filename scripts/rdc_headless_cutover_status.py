from __future__ import annotations

import json
from pathlib import Path

import win32com.client  # type: ignore

RUNTIME=Path(r"C:\ProgramData\ReqSys\RdcSvc")
RECEIPT=RUNTIME/"cutover-receipt.json"
NODE_RECEIPT=RUNTIME/"node-probe-receipt.json"
LOG=RUNTIME/"rdc-headless.log"
TASK_FOLDER=r"\Automation"
NAMES=("RemoteDesktopCommander","RemoteDesktopCommanderHeadless")


def task_info(folder,name:str)->dict[str,object]:
    try:
        task=folder.GetTask(name)
        d=task.Definition
        return {
            "present":True,
            "enabled":bool(task.Enabled),
            "state":int(task.State),
            "last_result":int(task.LastTaskResult),
            "principal_user":str(d.Principal.UserId or ""),
            "principal_logon_type":int(d.Principal.LogonType),
            "principal_run_level":int(d.Principal.RunLevel),
            "triggers":[int(t.Type) for t in d.Triggers],
            "actions":[{
                "type":int(a.Type),
                "path":str(getattr(a,"Path","") or ""),
                "arguments":str(getattr(a,"Arguments","") or ""),
            } for a in d.Actions],
        }
    except Exception as exc:
        return {"present":False,"error_type":type(exc).__name__}


def main()->int:
    result:dict[str,object]={
        "receipt_present":RECEIPT.is_file(),
        "node_probe_receipt_present":NODE_RECEIPT.is_file(),
        "log_present":LOG.is_file(),
        "secret_value_exposed":False,
    }
    if RECEIPT.is_file():
        try:
            r=json.loads(RECEIPT.read_text(encoding="utf-8"))
            for key in (
                "result","stage","error_type","rollback_new_task_attempted",
                "rollback_legacy_state_attempted","rollback_session_copy_attempted"
            ):
                result[f"receipt_{key}"]=r.get(key)
        except Exception as exc:
            result["receipt_read_error_type"]=type(exc).__name__
    if NODE_RECEIPT.is_file():
        try:
            r=json.loads(NODE_RECEIPT.read_text(encoding="utf-8"))
            for key in (
                "result","reason","node_path_system_level","receipt_present",
                "task_state","last_task_result","node_version_present",
                "username_matches_service","profile_present"
            ):
                result[f"node_probe_{key}"]=r.get(key)
        except Exception as exc:
            result["node_probe_read_error_type"]=type(exc).__name__
    if LOG.is_file():
        text=LOG.read_text(encoding="utf-8",errors="replace")
        result["log_session_restored"]="Session restored" in text
        result["log_device_ready"]="Device ready:" in text
        result["log_authenticating"]="Authenticating with Remote MCP server" in text
        result["log_startup_failed"]="Device startup failed" in text
        result["log_persisted_invalid"]="Persisted session invalid" in text
        result["log_size"]=LOG.stat().st_size
    app_pkg=RUNTIME/"app"/"node_modules"/"@wonderwhy-er"/"desktop-commander"/"package.json"
    result["package_present"]=app_pkg.is_file()
    if app_pkg.is_file():
        try:
            result["package_version"]=json.loads(app_pkg.read_text(encoding="utf-8")).get("version")
        except Exception as exc:
            result["package_read_error_type"]=type(exc).__name__
    service_profile=Path(r"C:\Users\ReqSysRdcSvc")
    secret_file=service_profile/".desktop-commander-device"/"device.json"
    result["service_profile_present"]=service_profile.is_dir()
    result["service_session_file_present"]=secret_file.is_file()
    if secret_file.is_file():
        result["service_session_file_size"]=secret_file.stat().st_size
    service=win32com.client.Dispatch("Schedule.Service")
    service.Connect()
    try:
        folder=service.GetFolder(TASK_FOLDER)
        result["tasks"]={name:task_info(folder,name) for name in NAMES}
    except Exception as exc:
        result["task_folder_error_type"]=type(exc).__name__
    print(json.dumps(result,ensure_ascii=True,sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())

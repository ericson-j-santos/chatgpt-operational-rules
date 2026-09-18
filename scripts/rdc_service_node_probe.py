from __future__ import annotations

import json
import shutil
import socket
import time
from pathlib import Path

import win32com.client  # type: ignore
import win32cred  # type: ignore

ACCOUNT="ReqSysRdcSvc"
CRED_TARGET="ReqSys/RdcSvc/TaskScheduler"
TASK_FOLDER=r"\Automation"
TASK_NAME="ReqSysRdcSvcNodeProbe"
RUNTIME=Path(r"C:\ProgramData\ReqSys\RdcSvc")
JS=RUNTIME/"node-probe.cjs"
OUT=RUNTIME/"node-probe.json"

TASK_CREATE_OR_UPDATE=6
TASK_LOGON_PASSWORD=1
TASK_RUNLEVEL_LUA=0
TASK_ACTION_EXEC=0


def credential()->tuple[str,str]:
    item=win32cred.CredRead(CRED_TARGET,win32cred.CRED_TYPE_GENERIC,0)
    user=str(item.get("UserName") or "")
    blob=item.get("CredentialBlob")
    if isinstance(blob,bytes):
        try:
            pwd=blob.decode("utf-16-le")
        except UnicodeDecodeError:
            pwd=blob.decode("utf-8")
    else:
        pwd=str(blob or "")
    if not user or not pwd:
        raise RuntimeError("managed credential unavailable")
    return user,pwd


def main()->int:
    node=shutil.which("node.exe") or shutil.which("node")
    if not node:
        print(json.dumps({"result":"blocked","reason":"node_missing","secret_value_exposed":False},sort_keys=True))
        return 2
    RUNTIME.mkdir(parents=True,exist_ok=True)
    OUT.unlink(missing_ok=True)
    JS.write_text(
        "const fs=require('fs');"
        f"fs.writeFileSync({json.dumps(str(OUT))},JSON.stringify({{ok:true,node:process.version,user:process.env.USERNAME||'',profile:!!process.env.USERPROFILE}}));",
        encoding="utf-8",
    )
    user,pwd=credential()
    svc=win32com.client.Dispatch("Schedule.Service")
    svc.Connect()
    try:
        folder=svc.GetFolder(TASK_FOLDER)
    except Exception:
        folder=svc.GetFolder("\\").CreateFolder(TASK_FOLDER.strip("\\"))
    try:
        folder.DeleteTask(TASK_NAME,0)
    except Exception:
        pass

    d=svc.NewTask(0)
    d.RegistrationInfo.Description="ReqSys RDC service Node execution probe"
    d.Settings.Enabled=True
    d.Settings.AllowDemandStart=True
    d.Settings.ExecutionTimeLimit="PT2M"
    d.Principal.UserId=user
    d.Principal.LogonType=TASK_LOGON_PASSWORD
    d.Principal.RunLevel=TASK_RUNLEVEL_LUA
    a=d.Actions.Create(TASK_ACTION_EXEC)
    a.Path=node
    a.Arguments=f'"{JS}"'
    a.WorkingDirectory=str(RUNTIME)
    task=folder.RegisterTaskDefinition(TASK_NAME,d,TASK_CREATE_OR_UPDATE,user,pwd,TASK_LOGON_PASSWORD)
    task.Run("")
    deadline=time.time()+30
    while time.time()<deadline and not OUT.exists():
        time.sleep(0.5)
    state=int(task.State)
    last=int(task.LastTaskResult)
    payload={
        "result":"ready" if OUT.exists() and last==0 else "blocked",
        "node_path_system_level":str(node).casefold().startswith(r"c:\program files"),
        "receipt_present":OUT.exists(),
        "task_state":state,
        "last_task_result":last,
        "secret_value_exposed":False,
    }
    if OUT.exists():
        try:
            data=json.loads(OUT.read_text(encoding="utf-8"))
            payload["node_version_present"]=bool(data.get("node"))
            payload["username_matches_service"]=str(data.get("user","")).casefold()==ACCOUNT.casefold()
            payload["profile_present"]=bool(data.get("profile"))
        except Exception as exc:
            payload["receipt_error_type"]=type(exc).__name__
    try:
        folder.DeleteTask(TASK_NAME,0)
    except Exception:
        pass
    OUT.unlink(missing_ok=True)
    JS.unlink(missing_ok=True)
    print(json.dumps(payload,ensure_ascii=True,sort_keys=True))
    return 0 if payload["result"]=="ready" else 2


if __name__=="__main__":
    raise SystemExit(main())

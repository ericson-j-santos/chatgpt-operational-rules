#!/usr/bin/env python3
"""Trigger the existing RemoteDesktopCommander scheduled task on DESKTOP-PDQK954.

Strictly allowlisted host/task. No credentials are supplied or read. The script
only queries and runs an existing task; it never creates/updates/deletes tasks.
"""
from __future__ import annotations
import argparse, json, subprocess, time

SAFE_HOST="DESKTOP-PDQK954"
SAFE_TASK=r"\Automation\RemoteDesktopCommander"

def run(args:list[str],timeout:int=30)->subprocess.CompletedProcess[str]:
    return subprocess.run(args,text=True,capture_output=True,encoding="utf-8",errors="replace",shell=False,timeout=timeout,check=False)

def classify(cp:subprocess.CompletedProcess[str])->str|None:
    text=((cp.stdout or "")+"\n"+(cp.stderr or "")).casefold()
    if cp.returncode==0:
        return None
    if "access is denied" in text or "acesso negado" in text:
        return "access_denied"
    if "cannot find" in text or "não é possível localizar" in text or "nao e possivel localizar" in text:
        return "task_not_found"
    if "rpc server" in text or "servidor rpc" in text:
        return "rpc_unavailable"
    return "command_failed"

def main()->int:
    p=argparse.ArgumentParser()
    p.add_argument("--host",default=SAFE_HOST)
    p.add_argument("--task",default=SAFE_TASK)
    p.add_argument("--run",action="store_true")
    ns=p.parse_args()
    if ns.host!=SAFE_HOST or ns.task!=SAFE_TASK:
        raise SystemExit("target_not_allowlisted")
    query=run(["schtasks.exe","/Query","/S",ns.host,"/TN",ns.task,"/FO","CSV","/NH"])
    payload={"host":ns.host,"task":ns.task,"query_returncode":query.returncode,
             "query_error":classify(query),"operation":"inspect","secret_values_exposed":False}
    if query.returncode!=0:
        print(json.dumps(payload,sort_keys=True))
        return 10
    payload["task_exists"]=True
    if ns.run:
        started=run(["schtasks.exe","/Run","/S",ns.host,"/TN",ns.task])
        payload.update({"operation":"run","run_returncode":started.returncode,"run_error":classify(started)})
        if started.returncode==0:
            time.sleep(3)
        print(json.dumps(payload,sort_keys=True))
        return 0 if started.returncode==0 else 11
    print(json.dumps(payload,sort_keys=True))
    return 0

if __name__=="__main__":
    raise SystemExit(main())

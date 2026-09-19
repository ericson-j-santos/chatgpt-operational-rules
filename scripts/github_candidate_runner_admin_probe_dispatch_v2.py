#!/usr/bin/env python3
"""Dispatch candidate runner-admin probe via GitHub API with sanitized status only."""
from __future__ import annotations
import json, shutil, subprocess, time

REPO="ericson-j-santos/reqsys-v2-enterprise-real"
WORKFLOW="github-workflow-permission-readiness-watch.yml"
REF="diag/runner-admin-token-1760-20260919"

def run(args:list[str], *, input_text:str|None=None, timeout:int=30)->subprocess.CompletedProcess[str]:
    return subprocess.run(args,input=input_text,text=True,capture_output=True,encoding="utf-8",errors="replace",shell=False,timeout=timeout,check=False)

def main()->int:
    gh=shutil.which("gh")
    if not gh:
        print(json.dumps({"status":"blocked","reason":"gh_missing","secret_values_exposed":False},sort_keys=True))
        return 3
    body=json.dumps({
        "ref":REF,
        "inputs":{
            "enforce":"false",
            "runner_admin_probe":"false",
            "app_runner_admin_probe":"false",
            "candidate_runner_admin_probe":"true",
        },
    })
    dispatched=run([gh,"api","--method","POST",
                    f"repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches",
                    "--input","-"],input_text=body)
    if dispatched.returncode!=0:
        stderr=(dispatched.stderr or "").lower()
        if "422" in stderr:
            reason="http_422"
        elif "403" in stderr:
            reason="http_403"
        elif "404" in stderr:
            reason="http_404"
        else:
            reason="dispatch_failed"
        print(json.dumps({"status":"blocked","reason":reason,"returncode":dispatched.returncode,"secret_values_exposed":False},sort_keys=True))
        return 4
    time.sleep(3)
    listed=run([gh,"run","list","--repo",REPO,"--workflow",WORKFLOW,"--branch",REF,
                "--event","workflow_dispatch","--limit","20",
                "--json","databaseId,url,headSha,status,conclusion,createdAt"])
    if listed.returncode!=0:
        print(json.dumps({"status":"blocked","reason":"list_failed","secret_values_exposed":False},sort_keys=True))
        return 5
    try:
        rows=json.loads(listed.stdout or "[]")
    except json.JSONDecodeError:
        rows=[]
    if not rows:
        print(json.dumps({"status":"blocked","reason":"run_not_found","secret_values_exposed":False},sort_keys=True))
        return 6
    row=rows[0]
    print(json.dumps({"status":"dispatched","run_id":row.get("databaseId"),"run_url":row.get("url"),
                      "head_sha":row.get("headSha"),"run_status":row.get("status"),
                      "conclusion":row.get("conclusion"),"secret_values_exposed":False},sort_keys=True))
    return 0

if __name__=="__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Probe whether local GitHub CLI auth can mint a repository runner registration token.

The token is parsed in memory and discarded. It is never printed or persisted.
"""
from __future__ import annotations
import argparse, json, shutil, subprocess

REPO="ericson-j-santos/reqsys-v2-enterprise-real"


def run(args:list[str],timeout:int=30)->subprocess.CompletedProcess[str]:
    return subprocess.run(args,text=True,capture_output=True,encoding="utf-8",errors="replace",shell=False,timeout=timeout,check=False)


def main()->int:
    p=argparse.ArgumentParser()
    p.add_argument("--confirm")
    p.add_argument("--status-only", action="store_true")
    ns=p.parse_args()
    if not ns.status_only and ns.confirm!="PROBE-RUNNER-REGISTRATION-TOKEN-DEV":
        raise SystemExit("confirmation_mismatch")
    gh=shutil.which("gh")
    if not gh:
        print(json.dumps({"gh_present":False,"registration_token_created":False,"secret_values_exposed":False},sort_keys=True))
        return 3
    who=run([gh,"api","user","--jq",".login"])
    identity=who.stdout.strip() if who.returncode==0 else None
    if ns.status_only:
        print(json.dumps({"gh_present":True,"authenticated":who.returncode==0,"identity":identity,"secret_values_exposed":False},sort_keys=True))
        return 0 if who.returncode==0 else 5
    req=run([gh,"api","--method","POST",f"repos/{REPO}/actions/runners/registration-token"])
    result={"gh_present":True,"identity":identity,"request_returncode":req.returncode,"registration_token_created":False,"expires_at":None,"secret_values_exposed":False}
    if req.returncode==0:
        try:
            payload=json.loads(req.stdout)
            token=payload.get("token")
            result["registration_token_created"]=bool(token)
            result["expires_at"]=payload.get("expires_at")
            token=None
        except json.JSONDecodeError:
            result["parse_error"]=True
    else:
        stderr=(req.stderr or "")
        if "HTTP 401" in stderr:
            result["error"]="unauthorized"; code=11
        elif "HTTP 403" in stderr:
            result["error"]="forbidden"; code=12
        elif "HTTP 404" in stderr:
            result["error"]="not_found"; code=13
        else:
            result["error"]="github_api_request_failed"; code=14
        print(json.dumps(result,sort_keys=True))
        return code
    print(json.dumps(result,sort_keys=True))
    return 0 if result["registration_token_created"] else 15


if __name__=="__main__":
    raise SystemExit(main())

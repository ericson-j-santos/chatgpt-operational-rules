#!/usr/bin/env python3
"""Probe repository admin capability using Git Credential Manager in-memory.

The credential is obtained via `git credential fill`, used only for one HTTPS
request, never printed, persisted, or passed on a command line. The runner
registration token returned by GitHub is also discarded in-memory.
"""
from __future__ import annotations
import argparse, json, subprocess, urllib.error, urllib.request

REPO="ericson-j-santos/reqsys-v2-enterprise-real"


def main()->int:
    p=argparse.ArgumentParser()
    p.add_argument("--confirm",required=True)
    ns=p.parse_args()
    if ns.confirm!="PROBE-GIT-CREDENTIAL-RUNNER-ADMIN-DEV":
        raise SystemExit("confirmation_mismatch")
    cp=subprocess.run(
        ["git","credential","fill"],
        input="protocol=https\nhost=github.com\nusername=ericson-j-santos\n\n",
        text=True,capture_output=True,encoding="utf-8",errors="replace",
        shell=False,timeout=30,check=False,
    )
    fields={}
    if cp.returncode==0:
        for line in cp.stdout.splitlines():
            if "=" in line:
                k,v=line.split("=",1)
                fields[k]=v
    credential=fields.get("password","")
    identity=fields.get("username")
    if not credential:
        print(json.dumps({"credential_present":False,"identity":identity,"registration_token_created":False,"secret_values_exposed":False},sort_keys=True))
        return 21
    url=f"https://api.github.com/repos/{REPO}/actions/runners/registration-token"
    req=urllib.request.Request(url,method="POST",headers={
        "Accept":"application/vnd.github+json",
        "Authorization":f"Bearer {credential}",
        "X-GitHub-Api-Version":"2026-03-10",
        "User-Agent":"ReqSys-PC24x7-Runner-Provisioner",
    })
    credential=None
    try:
        with urllib.request.urlopen(req,timeout=20) as resp:
            data=json.loads(resp.read().decode("utf-8"))
            token=data.get("token")
            out={"credential_present":True,"identity":identity,"http_status":resp.status,"registration_token_created":bool(token),"expires_at":data.get("expires_at"),"secret_values_exposed":False}
            token=None
            print(json.dumps(out,sort_keys=True))
            return 0 if out["registration_token_created"] else 25
    except urllib.error.HTTPError as exc:
        code={401:22,403:23,404:24}.get(exc.code,26)
        print(json.dumps({"credential_present":True,"identity":identity,"http_status":exc.code,"registration_token_created":False,"secret_values_exposed":False},sort_keys=True))
        return code


if __name__=="__main__":
    raise SystemExit(main())

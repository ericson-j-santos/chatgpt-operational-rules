#!/usr/bin/env python3
from __future__ import annotations
import json, os, subprocess, urllib.error, urllib.request

OWNER="ericson-j-santos"
REPO="central-pesquisas-habitacionais"
BRANCH="main"

def credential():
    env=dict(os.environ); env["GIT_TERMINAL_PROMPT"]="0"
    p=subprocess.run(["git","credential","fill"],input="protocol=https\nhost=github.com\n\n",text=True,capture_output=True,timeout=15,check=False,env=env)
    if p.returncode != 0:
        raise SystemExit(json.dumps({"result":"BLOCKED","reason":"credential unavailable"}))
    vals={}
    for line in p.stdout.splitlines():
        if "=" in line:
            k,v=line.split("=",1); vals[k.strip()]=v.strip()
    token=vals.get("password","")
    if not token:
        raise SystemExit(json.dumps({"result":"BLOCKED","reason":"credential unavailable"}))
    return token

def get(path, token):
    req=urllib.request.Request("https://api.github.com"+path,method="GET",headers={
        "Accept":"application/vnd.github+json",
        "Authorization":f"Bearer {token}",
        "X-GitHub-Api-Version":"2022-11-28",
        "User-Agent":"chatgpt-governed-branch-protection-diagnostic/1.0",
    })
    try:
        with urllib.request.urlopen(req,timeout=30) as r:
            raw=r.read().decode("utf-8")
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw=e.read().decode("utf-8",errors="replace")
        try: data=json.loads(raw) if raw else {}
        except json.JSONDecodeError: data={}
        return e.code,data

def main():
    t=credential()
    s,repo=get(f"/repos/{OWNER}/{REPO}",t)
    ps,prot=get(f"/repos/{OWNER}/{REPO}/branches/{BRANCH}/protection",t)
    print(json.dumps({
        "repo_http":s,
        "full_name":repo.get("full_name"),
        "private":repo.get("private"),
        "permissions":repo.get("permissions"),
        "protection_http":ps,
        "protection_message":str(prot.get("message",""))[:200],
        "already_protected":ps==200,
    },sort_keys=True))
    return 0

if __name__=="__main__":
    raise SystemExit(main())

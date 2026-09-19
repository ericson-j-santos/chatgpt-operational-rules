#!/usr/bin/env python3
"""Governed remote control for an existing GitHub Actions runner Windows service.

Scope is restricted to DESKTOP-PDQK954 and service names prefixed by actions.runner.
The script never reads runner credential files or emits secret values.
"""
from __future__ import annotations
import argparse, json, re, subprocess, time

SAFE_HOST="DESKTOP-PDQK954"
SERVICE_RE=re.compile(r"(?i)SERVICE_NAME:\s*(actions\.runner\.[^\r\n]+)")


def run(args:list[str], timeout:int=30)->subprocess.CompletedProcess[str]:
    return subprocess.run(args,text=True,capture_output=True,encoding="utf-8",errors="replace",shell=False,timeout=timeout,check=False)


def list_services(host:str)->list[dict]:
    target=f"\\\\{host}"
    q=run(["sc.exe",target,"query","type=","service","state=","all"])
    names=sorted(set(SERVICE_RE.findall(q.stdout if q.returncode==0 else "")),key=str.casefold)
    rows=[]
    for name in names:
        s=run(["sc.exe",target,"query",name])
        qc=run(["sc.exe",target,"qc",name])
        state=re.search(r"(?im)^\s*STATE\s*:\s*\d+\s+(\w+)",s.stdout)
        start=re.search(r"(?im)^\s*START_TYPE\s*:\s*\d+\s+(\w+)",qc.stdout)
        rows.append({"name":name,"state":state.group(1).upper() if state else "UNKNOWN","start_type":start.group(1).upper() if start else "UNKNOWN","query_rc":s.returncode,"qc_rc":qc.returncode})
    return rows


def main()->int:
    p=argparse.ArgumentParser()
    p.add_argument("--host",default=SAFE_HOST)
    p.add_argument("--start",action="store_true")
    p.add_argument("--service")
    ns=p.parse_args()
    if ns.host!=SAFE_HOST:
        raise SystemExit("host_not_allowlisted")
    before=list_services(ns.host)
    payload={"host":ns.host,"before":before,"operation":"inspect","secret_values_exposed":False}
    if ns.start:
        if not ns.service or not ns.service.lower().startswith("actions.runner."):
            raise SystemExit("service_not_allowlisted")
        if ns.service not in {x["name"] for x in before}:
            raise SystemExit("service_not_found")
        target=f"\\\\{ns.host}"
        started=run(["sc.exe",target,"start",ns.service])
        time.sleep(2)
        after=list_services(ns.host)
        payload.update({"operation":"start","start_rc":started.returncode,"after":after})
    print(json.dumps(payload,sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())

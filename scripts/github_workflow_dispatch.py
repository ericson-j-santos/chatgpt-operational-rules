#!/usr/bin/env python3
"""Disparo governado e restrito de GitHub Actions via API."""
from __future__ import annotations
import argparse, json, os, re, sys, urllib.error, urllib.request, uuid
from datetime import datetime, timezone
from pathlib import Path

EXIT_POLICY=20
EXIT_REMOTE=22
SAFE=re.compile(r"^[A-Za-z0-9._/-]+$")

def load_policy(path: Path) -> dict:
    data=json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("allowed_dispatches"), list):
        raise ValueError("política de dispatch inválida")
    return data

def allowed(policy: dict, repo: str, workflow: str, ref: str) -> bool:
    return any(x.get("repository")==repo and x.get("workflow")==workflow and ref in x.get("refs",[]) for x in policy["allowed_dispatches"])

def dispatch(repo: str, workflow: str, ref: str, token: str, correlation_id: str) -> dict:
    url=f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches"
    req=urllib.request.Request(url,data=json.dumps({"ref":ref}).encode(),method="POST",headers={"Accept":"application/vnd.github+json","Authorization":f"Bearer {token}","X-GitHub-Api-Version":"2022-11-28","User-Agent":"ReqSys-Governed-Dispatch/1.0"})
    with urllib.request.urlopen(req,timeout=30) as response:
        if response.status != 204: raise RuntimeError(f"status inesperado: {response.status}")
    return {"timestamp":datetime.now(timezone.utc).isoformat(),"correlation_id":correlation_id,"result":"DISPATCH_ACCEPTED","repository":repo,"workflow":workflow,"ref":ref}

def main() -> int:
    p=argparse.ArgumentParser(); p.add_argument("--policy",type=Path,required=True); p.add_argument("--repository",required=True); p.add_argument("--workflow",required=True); p.add_argument("--ref",required=True); p.add_argument("--correlation-id")
    ns=p.parse_args(); cid=ns.correlation_id or f"dispatch-{uuid.uuid4().hex[:12]}"
    try:
        if not all(SAFE.fullmatch(x) for x in (ns.repository,ns.workflow,ns.ref)): raise ValueError("argumento inválido")
        policy=load_policy(ns.policy)
        if not allowed(policy,ns.repository,ns.workflow,ns.ref): raise ValueError("dispatch não autorizado pela allowlist")
        token=os.environ.get("GITHUB_TOKEN")
        if not token: raise ValueError("GITHUB_TOKEN ausente")
        print(json.dumps(dispatch(ns.repository,ns.workflow,ns.ref,token,cid),ensure_ascii=False)); return 0
    except (ValueError,OSError,urllib.error.URLError,RuntimeError) as exc:
        print(json.dumps({"correlation_id":cid,"result":"DISPATCH_BLOCKED","error":str(exc)}),file=sys.stderr); return EXIT_POLICY if isinstance(exc,ValueError) else EXIT_REMOTE
if __name__=="__main__": raise SystemExit(main())

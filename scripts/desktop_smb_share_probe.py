#!/usr/bin/env python3
"""Read-only SMB share enumeration for DESKTOP-PDQK954."""
from __future__ import annotations
import json, os

SAFE_HOST="DESKTOP-PDQK954"

def main()->int:
    if os.name!="nt":
        print(json.dumps({"host":SAFE_HOST,"windows":False,"shares":[],"secret_values_exposed":False},sort_keys=True)); return 2
    try:
        import win32net
        data,total,resume=win32net.NetShareEnum(SAFE_HOST,1)
        shares=[]
        for item in data:
            name=str(item.get("netname") or "")
            stype=int(item.get("type") or 0)
            shares.append({"name":name,"type":stype,"hidden":name.endswith("$")})
        print(json.dumps({"host":SAFE_HOST,"enumerate_ok":True,"shares":shares,"secret_values_exposed":False},sort_keys=True))
        return 0
    except Exception as exc:
        msg=str(exc).casefold()
        if "access is denied" in msg or "acesso negado" in msg:
            cls="access_denied"
        else:
            cls=type(exc).__name__
        print(json.dumps({"host":SAFE_HOST,"enumerate_ok":False,"error_class":cls,"shares":[],"secret_values_exposed":False},sort_keys=True))
        return 3

if __name__=="__main__":
    raise SystemExit(main())

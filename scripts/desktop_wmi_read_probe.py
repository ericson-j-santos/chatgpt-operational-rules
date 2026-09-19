#!/usr/bin/env python3
"""Read-only WMI/DCOM capability probe for the authorized Desktop host."""
from __future__ import annotations
import json, os, socket

SAFE_HOST="DESKTOP-PDQK954"

def main()->int:
    if os.name!="nt":
        print(json.dumps({"host":SAFE_HOST,"windows":False,"wmi_ok":False,"secret_values_exposed":False},sort_keys=True))
        return 2
    try:
        import win32com.client
        locator=win32com.client.Dispatch("WbemScripting.SWbemLocator")
        svc=locator.ConnectServer(SAFE_HOST, r"root\cimv2")
        rows=svc.ExecQuery("SELECT CSName, Caption, Version FROM Win32_OperatingSystem")
        items=[]
        for row in rows:
            items.append({"computer_name":str(row.CSName),"caption":str(row.Caption),"version":str(row.Version)})
        print(json.dumps({"host":SAFE_HOST,"wmi_ok":True,"items":items,"secret_values_exposed":False},sort_keys=True))
        return 0
    except Exception as exc:
        msg=str(exc).casefold()
        if "access is denied" in msg or "acesso negado" in msg or "0x80070005" in msg:
            cls="access_denied"
        elif "rpc" in msg or "0x800706ba" in msg:
            cls="rpc_unavailable"
        else:
            cls=type(exc).__name__
        print(json.dumps({"host":SAFE_HOST,"wmi_ok":False,"error_class":cls,"secret_values_exposed":False},sort_keys=True))
        return 3

if __name__=="__main__":
    raise SystemExit(main())

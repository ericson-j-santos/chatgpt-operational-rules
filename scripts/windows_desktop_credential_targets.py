#!/usr/bin/env python3
"""List only matching Windows Credential Manager target names for DESKTOP-PDQK954.

No username, password, credential blob or secret value is read or emitted.
"""
from __future__ import annotations
import ctypes, json
from ctypes import wintypes

MATCHES=("desktop-pdqk954","192.168.1.45")
CRED_ENUMERATE_ALL_CREDENTIALS=0x1

class CREDENTIALW(ctypes.Structure):
    _fields_=[
        ("Flags",wintypes.DWORD),("Type",wintypes.DWORD),("TargetName",wintypes.LPWSTR),
        ("Comment",wintypes.LPWSTR),("LastWritten",wintypes.FILETIME),
        ("CredentialBlobSize",wintypes.DWORD),("CredentialBlob",ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist",wintypes.DWORD),("AttributeCount",wintypes.DWORD),
        ("Attributes",ctypes.c_void_p),("TargetAlias",wintypes.LPWSTR),("UserName",wintypes.LPWSTR),
    ]

def main()->int:
    if not hasattr(ctypes,"WinDLL"):
        print(json.dumps({"windows":False,"matches":[],"secret_values_exposed":False},sort_keys=True))
        return 2
    adv=ctypes.WinDLL("Advapi32.dll",use_last_error=True)
    enum=adv.CredEnumerateW
    enum.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD),ctypes.POINTER(ctypes.POINTER(ctypes.POINTER(CREDENTIALW)))]
    enum.restype=wintypes.BOOL
    free=adv.CredFree
    count=wintypes.DWORD()
    items=ctypes.POINTER(ctypes.POINTER(CREDENTIALW))()
    ok=enum(None,CRED_ENUMERATE_ALL_CREDENTIALS,ctypes.byref(count),ctypes.byref(items))
    if not ok:
        print(json.dumps({"windows":True,"enumerate_ok":False,"matches":[],"winerror":ctypes.get_last_error(),"secret_values_exposed":False},sort_keys=True))
        return 3
    found=[]
    try:
        for i in range(count.value):
            cred=items[i].contents
            target=cred.TargetName or ""
            folded=target.casefold()
            if any(m in folded for m in MATCHES):
                found.append({"target":target,"type":int(cred.Type),"persist":int(cred.Persist)})
    finally:
        free(items)
    print(json.dumps({"windows":True,"enumerate_ok":True,"match_count":len(found),"matches":found,"secret_values_exposed":False},sort_keys=True))
    return 0

if __name__=="__main__":
    raise SystemExit(main())

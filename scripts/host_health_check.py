#!/usr/bin/env python3
"""Restricted Windows host health check for governed ReqSys execution.

The helper exposes only boolean state. It never reads or emits credential
material, DSNs, tokens, usernames, or passwords.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
from ctypes import wintypes

GATEWAY_IMAGE = "Microsoft.PowerBI.EnterpriseGateway.exe"
CREDENTIAL_TARGET = "ReqSys/PowerPlatform/SQL-DEV"


class CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
    ]


def gateway_process_active() -> bool:
    if os.name != "nt":
        return False
    completed = subprocess.run(
        ["tasklist.exe", "/FI", f"IMAGENAME eq {GATEWAY_IMAGE}", "/FO", "CSV", "/NH"],
        text=True, capture_output=True, encoding="utf-8", errors="replace",
        shell=False, timeout=10, check=False,
    )
    return completed.returncode == 0 and GATEWAY_IMAGE.casefold() in completed.stdout.casefold()


def credential_present() -> bool:
    if os.name != "nt":
        return False
    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    cred_read = advapi32.CredReadW
    cred_read.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                          ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
    cred_read.restype = wintypes.BOOL
    cred_free = advapi32.CredFree
    cred_free.argtypes = [ctypes.c_void_p]
    cred_free.restype = None
    ptr = ctypes.POINTER(CREDENTIALW)()
    ok = bool(cred_read(CREDENTIAL_TARGET, 1, 0, ctypes.byref(ptr)))
    if ok and ptr:
        cred_free(ptr)
    return ok


def collect() -> dict[str, bool]:
    return {
        "gateway_process_active": gateway_process_active(),
        "credential_present": credential_present(),
    }


def main() -> int:
    argparse.ArgumentParser(description="Restricted ReqSys host health check").parse_args()
    print(json.dumps(collect(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

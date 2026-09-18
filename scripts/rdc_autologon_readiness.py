#!/usr/bin/env python3
"""Read-only Windows AutoLogon readiness metadata. Never reads password contents."""
from __future__ import annotations

import json
import os
import sys

try:
    import winreg
except ImportError:
    print(json.dumps({"result": "blocked", "error": "winreg unavailable"}))
    raise SystemExit(2)

WINLOGON = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"


def read_value(key, name: str):
    try:
        return winreg.QueryValueEx(key, name)[0]
    except FileNotFoundError:
        return None


def main() -> int:
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, WINLOGON, 0, winreg.KEY_READ) as key:
            auto = str(read_value(key, "AutoAdminLogon") or "").strip()
            user = str(read_value(key, "DefaultUserName") or "").strip()
            domain = str(read_value(key, "DefaultDomainName") or "").strip()
            # Presence only. The secret value is deliberately never queried.
            try:
                winreg.QueryValueEx(key, "DefaultPassword")
                password_marker_present = True
            except FileNotFoundError:
                password_marker_present = False

        payload = {
            "host": os.environ.get("COMPUTERNAME", ""),
            "auto_admin_logon_enabled": auto == "1",
            "default_user_configured": bool(user),
            "default_domain_configured": bool(domain),
            "registry_password_marker_present": password_marker_present,
            "secret_value_read": False,
            "note": "A missing registry password marker does not rule out an LSA-protected AutoLogon secret.",
        }
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
        return 0
    except OSError as exc:
        print(json.dumps({"result": "blocked", "error": type(exc).__name__, "secret_value_read": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

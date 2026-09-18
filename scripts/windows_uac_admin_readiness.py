from __future__ import annotations

import ctypes
import json
import os
import winreg

import win32net  # type: ignore

POLICIES=r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"


def reg(name:str):
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,POLICIES,0,winreg.KEY_READ) as key:
            return winreg.QueryValueEx(key,name)[0]
    except Exception:
        return None


def main()->int:
    user=os.environ.get("USERNAME","")
    try:
        groups=[str(x) for x in win32net.NetUserGetLocalGroups(None,user,0)]
    except Exception:
        groups=[]
    admin_member=any(g.casefold() in {"administrators","administradores"} for g in groups)
    enable_lua=reg("EnableLUA")
    consent_admin=reg("ConsentPromptBehaviorAdmin")
    secure_desktop=reg("PromptOnSecureDesktop")
    payload={
        "user_present":bool(user),
        "is_admin_token":bool(ctypes.windll.shell32.IsUserAnAdmin()),
        "member_of_local_administrators":admin_member,
        "enable_lua":enable_lua,
        "consent_prompt_behavior_admin":consent_admin,
        "prompt_on_secure_desktop":secure_desktop,
        "silent_runas_possible_by_policy":bool(admin_member and (enable_lua==0 or consent_admin==0)),
        "secret_value_exposed":False,
    }
    print(json.dumps(payload,ensure_ascii=True,sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Governed UI automation for GitHub device-flow authorization in Opera."""
from __future__ import annotations

import argparse
import json
import re
import time
from pywinauto import Desktop

CODE_RE = re.compile(r"^[A-Z0-9]{4}-[A-Z0-9]{4}$", re.I)

def windows():
    return Desktop(backend="uia").windows()

def find_gcm():
    for win in windows():
        try:
            if win.window_text().strip().casefold() == "device code authentication":
                return win
        except Exception:
            pass
    return None

def find_opera():
    for win in windows():
        try:
            title = win.window_text()
            if title and title.endswith(" - Opera"):
                return win
        except Exception:
            pass
    return None

def text(ctrl):
    try:
        return " ".join((ctrl.window_text() or "").split())
    except Exception:
        return ""

def snapshot(win, limit=160):
    items=[]
    for ctrl in win.descendants():
        try:
            ctype=ctrl.element_info.control_type
            if ctype not in {"Button","Hyperlink","Text","Document","Edit","TabItem"}:
                continue
            t=text(ctrl)
            if not t:
                continue
            if ctype=="Edit" and ("password" in t.casefold() or "senha" in t.casefold()):
                t="[REDACTED]"
            item={"type":ctype,"text":t[:220]}
            if item not in items:
                items.append(item)
            if len(items)>=limit:
                break
        except Exception:
            continue
    return items

def click_named(win, names):
    wanted={n.casefold() for n in names}
    for ctrl in win.descendants():
        try:
            if ctrl.element_info.control_type not in {"Button","Hyperlink"}:
                continue
            t=text(ctrl).casefold()
            if t in wanted:
                ctrl.invoke()
                return t
        except Exception:
            try:
                if text(ctrl).casefold() in wanted:
                    ctrl.click_input()
                    return text(ctrl).casefold()
            except Exception:
                continue
    return None

def main() -> int:
    p=argparse.ArgumentParser()
    p.add_argument("--stage", choices=["open","inspect","enter","approve"], required=True)
    p.add_argument("--code")
    args=p.parse_args()

    if args.stage=="open":
        win=find_gcm()
        if win is None:
            print(json.dumps({"result":"BLOCKED","error":"GCM window not found"}))
            return 1
        clicked=click_named(win,["https://github.com/login/device"])
        if not clicked:
            print(json.dumps({"result":"BLOCKED","error":"device URL button not found"}))
            return 1
        time.sleep(4)
        print(json.dumps({"result":"DEVICE_URL_OPENED"}))
        return 0

    opera=find_opera()
    if opera is None:
        print(json.dumps({"result":"BLOCKED","error":"Opera window not found"}))
        return 1

    if args.stage=="inspect":
        print(json.dumps({"result":"INSPECT_OK","title":text(opera),"controls":snapshot(opera)},ensure_ascii=False))
        return 0

    if args.stage=="enter":
        if not args.code or not CODE_RE.fullmatch(args.code):
            print(json.dumps({"result":"BLOCKED","error":"invalid device code"}))
            return 2
        # Require GitHub device page semantics before typing.
        controls=snapshot(opera)
        joined=" ".join(i["text"] for i in controls).casefold()
        if "github" not in joined or not any(k in joined for k in ("device", "activation", "código", "code")):
            print(json.dumps({"result":"BLOCKED","error":"GitHub device page not confirmed","controls":controls[:40]},ensure_ascii=False))
            return 1
        edits=[]
        for ctrl in opera.descendants():
            try:
                if ctrl.element_info.control_type=="Edit" and ctrl.is_enabled():
                    edits.append(ctrl)
            except Exception:
                pass
        if not edits:
            print(json.dumps({"result":"BLOCKED","error":"device code input not found"}))
            return 1
        target=edits[-1]
        try:
            target.set_edit_text(args.code.upper())
        except Exception:
            target.click_input()
            target.type_keys(args.code.upper(), with_spaces=False)
        clicked=click_named(opera,["Continue","Continuar","Submit","Enviar"])
        if not clicked:
            print(json.dumps({"result":"BLOCKED","error":"continue button not found"}))
            return 1
        time.sleep(4)
        print(json.dumps({"result":"DEVICE_CODE_SUBMITTED"}))
        return 0

    controls=snapshot(opera)
    joined=" ".join(i["text"] for i in controls).casefold()
    if "git credential manager" not in joined and "credential manager" not in joined:
        print(json.dumps({"result":"BLOCKED","error":"Git Credential Manager authorization page not confirmed","controls":controls[:60]},ensure_ascii=False))
        return 1
    clicked=click_named(opera,[
        "Authorize Git Credential Manager",
        "Authorize git-credential-manager",
        "Authorize",
        "Autorizar Git Credential Manager",
        "Autorizar",
    ])
    if not clicked:
        print(json.dumps({"result":"BLOCKED","error":"authorize button not found","controls":controls[:80]},ensure_ascii=False))
        return 1
    time.sleep(5)
    print(json.dumps({"result":"GCM_AUTHORIZED"}))
    return 0

if __name__=="__main__":
    raise SystemExit(main())

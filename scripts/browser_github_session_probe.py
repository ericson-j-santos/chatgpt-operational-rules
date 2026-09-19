#!/usr/bin/env python3
"""Probe an existing Chrome/Edge session for authenticated GitHub access without reading credentials."""
from __future__ import annotations
import argparse, json, time
from pywinauto import Desktop

def txt(ctrl):
    try: return " ".join((ctrl.window_text() or "").split())
    except Exception: return ""

def find_browser(browser: str):
    wins=Desktop(backend="uia").windows()
    preferred=[]
    for w in wins:
        try:
            t=txt(w)
            chrome=t.endswith(" - Google Chrome")
            edge=t.endswith(" - Microsoft Edge")
            if browser=="chrome" and chrome:
                preferred.append(w)
            elif browser=="edge" and edge:
                preferred.append(w)
            elif browser=="any" and (chrome or edge):
                preferred.append(w)
        except Exception:
            pass
    return preferred[0] if preferred else None

def click_named(win,names):
    wanted={x.casefold() for x in names}
    for c in win.descendants():
        try:
            if c.element_info.control_type=="Button" and txt(c).casefold() in wanted:
                try: c.invoke()
                except Exception: c.click_input()
                return True
        except Exception: pass
    return False

def snapshot(win, limit=140):
    out=[]
    for c in win.descendants():
        try:
            typ=c.element_info.control_type
            if typ not in {"Text","Hyperlink","Button","Document","TabItem"}: continue
            t=txt(c)
            if not t: continue
            item={"type":typ,"text":t[:240]}
            if item not in out: out.append(item)
            if len(out)>=limit: break
        except Exception: pass
    return out

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--url",required=True)
    p.add_argument("--browser",choices=["any","chrome","edge"],default="any")
    args=p.parse_args()
    win=find_browser(args.browser)
    if not win:
        print(json.dumps({"result":"BLOCKED","error":"Chrome/Edge window not found"}))
        return 1
    if not click_named(win,["Novo separador","Nova guia","New tab"]):
        try:
            win.set_focus(); win.type_keys("^l")
        except Exception:
            print(json.dumps({"result":"BLOCKED","error":"cannot activate address bar"}))
            return 1
    else:
        time.sleep(1)
        try:
            win.set_focus(); win.type_keys("^l")
        except Exception:
            pass
    try:
        win.type_keys(args.url, with_spaces=False)
        win.type_keys("{ENTER}")
    except Exception as exc:
        print(json.dumps({"result":"BLOCKED","error":f"navigation input failed: {type(exc).__name__}"}))
        return 1
    time.sleep(6)
    controls=snapshot(win)
    joined=" ".join(x["text"] for x in controls).casefold()
    if "page not found" in joined or "404" in joined:
        state="NOT_AUTHENTICATED_OR_NOT_FOUND"
    elif "sign in to github" in joined or ("sign in" in joined and "github" in joined):
        state="SIGN_IN_REQUIRED"
    elif "central-pesquisas-habitacionais" in joined and ("settings" in joined or "configura" in joined):
        state="AUTHENTICATED_REPO_SETTINGS"
    elif "central-pesquisas-habitacionais" in joined:
        state="AUTHENTICATED_REPO"
    else:
        state="UNKNOWN"
    print(json.dumps({"result":"PROBE_OK","state":state,"title":txt(win),"controls":controls},ensure_ascii=False))
    return 0

if __name__=="__main__":
    raise SystemExit(main())

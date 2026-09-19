#!/usr/bin/env python3
"""Read only the temporary GitHub device code from Git Credential Manager UI."""
from __future__ import annotations

import json
import re
from pywinauto import Desktop

CODE_RE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}\b", re.I)
URL_RE = re.compile(r"https://github\.com/login/device", re.I)

def candidates(ctrl):
    values = []
    for getter in (
        lambda: ctrl.window_text(),
        lambda: getattr(ctrl.element_info, "name", ""),
        lambda: ctrl.iface_value.CurrentValue if hasattr(ctrl, "iface_value") else "",
    ):
        try:
            value = getter()
            if value:
                values.append(str(value).strip())
        except Exception:
            pass
    return values

def main() -> int:
    desktop = Desktop(backend="uia")
    target = None
    for win in desktop.windows():
        try:
            if win.window_text().strip().casefold() == "device code authentication":
                target = win
                break
        except Exception:
            continue
    if target is None:
        print(json.dumps({"result":"BLOCKED","error":"device auth window not found"}))
        return 1

    codes = []
    urls = []
    controls = []
    for ctrl in target.descendants():
        try:
            ctype = ctrl.element_info.control_type
            texts = candidates(ctrl)
            for text in texts:
                m = CODE_RE.search(text)
                if m and m.group(0).upper() not in codes:
                    codes.append(m.group(0).upper())
                u = URL_RE.search(text)
                if u and u.group(0) not in urls:
                    urls.append(u.group(0))
            if texts:
                controls.append({"type":ctype,"text":" | ".join(texts)[:200]})
        except Exception:
            continue

    print(json.dumps({
        "result":"DEVICE_CODE_FOUND" if codes else "BLOCKED",
        "codes":codes,
        "urls":urls,
        "controls":controls[:80],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if codes else 1

if __name__ == "__main__":
    raise SystemExit(main())

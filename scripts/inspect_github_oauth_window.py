#!/usr/bin/env python3
"""Inspect visible GitHub/OAuth controls in browser windows without reading edit fields."""
from __future__ import annotations

import json
import re
import sys
from pywinauto import Desktop

SENSITIVE = re.compile(r"(?i)(password|senha|token|secret|cookie|authorization|otp|2fa|one[- ]time|passcode)")

def safe_text(value: str) -> str:
    text = " ".join((value or "").split())
    if not text:
        return ""
    if SENSITIVE.search(text):
        return "[REDACTED]"
    return text[:240]

def main() -> int:
    desktop = Desktop(backend="uia")
    result: list[dict] = []
    for win in desktop.windows():
        try:
            title = safe_text(win.window_text())
            proc_id = win.process_id()
            if not title:
                continue
            title_l = title.casefold()
            if not any(k in title_l for k in ("github", "opera", "credential", "sign in", "authorize", "device")):
                continue
            entry = {"title": title, "process_id": proc_id, "controls": []}
            for ctrl in win.descendants():
                try:
                    ctype = ctrl.element_info.control_type
                    if ctype not in {"Button", "Hyperlink", "Text", "Document", "TabItem"}:
                        continue
                    text = safe_text(ctrl.window_text())
                    if not text:
                        continue
                    item = {"type": ctype, "text": text}
                    if item not in entry["controls"]:
                        entry["controls"].append(item)
                    if len(entry["controls"]) >= 120:
                        break
                except Exception:
                    continue
            result.append(entry)
        except Exception:
            continue
    print(json.dumps({"windows": result}, ensure_ascii=False, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

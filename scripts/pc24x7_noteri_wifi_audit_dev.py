from __future__ import annotations

import json
import re
import subprocess


def run(args: list[str]) -> dict[str, object]:
    try:
        cp = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
        return {
            "rc": cp.returncode,
            "stdout": cp.stdout[-12000:],
            "stderr": cp.stderr[-2000:],
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    profiles = run(["netsh.exe", "wlan", "show", "profiles"])
    interfaces = run(["netsh.exe", "wlan", "show", "interfaces"])
    autoconfig = run(["netsh.exe", "wlan", "show", "autoconfig"])

    names: list[str] = []
    text = str(profiles.get("stdout", ""))
    for line in text.splitlines():
        if ":" not in line:
            continue
        left, right = line.split(":", 1)
        if "profile" in left.casefold() or "perfil" in left.casefold():
            name = right.strip()
            if name and name not in names:
                names.append(name)

    details: dict[str, object] = {}
    for name in names[:10]:
        details[name] = run(["netsh.exe", "wlan", "show", "profile", f"name={name}"])

    print(json.dumps({
        "profiles": profiles,
        "interfaces": interfaces,
        "autoconfig": autoconfig,
        "profile_names": names,
        "profile_details": details,
    }, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

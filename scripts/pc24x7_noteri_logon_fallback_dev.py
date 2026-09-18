from __future__ import annotations

import argparse
import json
import subprocess
import sys
import winreg
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = Path.home() / "AppData" / "Local" / "ReqSys" / "TodoGlobal24x7Noteri"
LAUNCHER = BASE / "noteri_logon_startup.py"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "TodoGlobal24x7NoteriHA"
WINLOGON_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"


def auto_admin_logon_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, WINLOGON_KEY) as key:
            value, _ = winreg.QueryValueEx(key, "AutoAdminLogon")
        return str(value).strip() == "1"
    except OSError:
        return False


def current_run_value() -> str:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE)
        return str(value)
    except OSError:
        return ""


def install() -> None:
    BASE.mkdir(parents=True, exist_ok=True)
    launcher = f'''from __future__ import annotations
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"{ROOT}")
raise SystemExit(subprocess.run(
    [sys.executable, "-m", "scripts.deploy_pc24x7_noteri_ha_worker_dev"],
    cwd=ROOT,
    check=False,
).returncode)
'''
    LAUNCHER.write_text(launcher, encoding="utf-8")
    command = f'"{sys.executable}" "{LAUNCHER}"'
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, command)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fallback de autostart no logon para o worker HA do Noteri")
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    if args.install:
        install()
    run_value = current_run_value()
    result = {
        "launcher_exists": LAUNCHER.is_file(),
        "run_value_present": bool(run_value),
        "auto_admin_logon": auto_admin_logon_enabled(),
        "automatic_after_reboot": bool(run_value) and LAUNCHER.is_file() and auto_admin_logon_enabled(),
    }
    print(json.dumps(result, sort_keys=True))
    return 0 if result["launcher_exists"] and result["run_value_present"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

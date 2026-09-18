from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASKS = {
    "desktop": "TodoGlobal24x7Headless",
    "noteri": "TodoGlobal24x7NoteriHA",
}


def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def audit_task(node: str) -> dict[str, object]:
    task_name = TASKS[node]
    cp = run(["schtasks.exe", "/Query", "/TN", task_name, "/XML"])
    if cp.returncode != 0:
        return {"node": node, "task_name": task_name, "exists": False, "query_rc": cp.returncode}
    root = ET.fromstring(cp.stdout)
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    def text(path: str) -> str:
        found = root.find(path, ns)
        return (found.text or "").strip() if found is not None else ""
    command = text(".//t:Actions/t:Exec/t:Command")
    arguments = text(".//t:Actions/t:Exec/t:Arguments")
    working_directory = text(".//t:Actions/t:Exec/t:WorkingDirectory")
    boot_trigger = root.find(".//t:Triggers/t:BootTrigger", ns) is not None
    return {
        "node": node,
        "task_name": task_name,
        "exists": True,
        "boot_trigger": boot_trigger,
        "logon_type": text(".//t:Principals/t:Principal/t:LogonType"),
        "run_level": text(".//t:Principals/t:Principal/t:RunLevel"),
        "command": Path(command).name if command else "",
        "arguments": arguments,
        "working_directory": working_directory,
    }


def install_desktop() -> None:
    runtime = Path.home() / "AppData" / "Local" / "ReqSys" / "TodoGlobal24x7"
    runtime.mkdir(parents=True, exist_ok=True)
    source = ROOT / "scripts" / "todo_gateway_windows_headless_boot.py"
    target = runtime / "headless_boot_runner.py"
    shutil.copy2(source, target)
    postboot = runtime / "postboot_runner.py"
    if not postboot.is_file():
        raise SystemExit(f"postboot runner missing: {postboot}")
    installer = ROOT / "scripts" / "install_todo_gateway_windows_s4u.ps1"
    cp = run([
        "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(installer),
        "-Python", sys.executable,
        "-RepoRoot", str(ROOT),
    ])
    if cp.returncode != 0:
        raise SystemExit((cp.stderr or cp.stdout)[-800:])


def install_noteri() -> None:
    installer = ROOT / "scripts" / "install_pc24x7_noteri_windows_s4u.ps1"
    cp = run([
        "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(installer),
        "-Python", sys.executable,
        "-RepoRoot", str(ROOT),
    ])
    if cp.returncode != 0:
        raise SystemExit((cp.stderr or cp.stdout)[-800:])


def main() -> int:
    parser = argparse.ArgumentParser(description="Instala/audita autostart S4U DEV do PC24x7")
    parser.add_argument("--node", choices=("desktop", "noteri", "all"), required=True)
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    nodes = ["desktop", "noteri"] if args.node == "all" else [args.node]
    if args.install:
        if args.node == "all":
            raise SystemExit("--install requires one concrete node")
        (install_desktop if args.node == "desktop" else install_noteri)()
    result = [audit_task(node) for node in nodes]
    for item in result:
        if item.get("exists"):
            item["valid"] = (
                item.get("boot_trigger") is True
                and item.get("logon_type") == "S4U"
                and item.get("run_level") in {"LeastPrivilege", "Limited"}
            )
        else:
            item["valid"] = False
    print(json.dumps(result if args.node == "all" else result[0], sort_keys=True))
    return 0 if all(bool(x["valid"]) for x in result) else 2


if __name__ == "__main__":
    raise SystemExit(main())

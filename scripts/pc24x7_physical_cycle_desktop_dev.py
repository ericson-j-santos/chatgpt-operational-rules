from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path

TARGET = "DESKTOP-PDQK954"


def main() -> int:
    host = socket.gethostname()
    if host.casefold() != TARGET.casefold():
        raise SystemExit(f"refusing physical cycle on unexpected host: {host}")
    delay_seconds = 10
    env = os.environ.copy()
    system_root = env.get("SystemRoot") or env.get("windir") or r"C:\\Windows"
    env["SystemRoot"] = system_root
    env["windir"] = system_root
    env.setdefault("USERDOMAIN", host)
    shutdown_exe = Path(system_root) / "System32" / "shutdown.exe"
    command = [str(shutdown_exe), "/r", "/t", str(delay_seconds)]
    started = subprocess.run(command, capture_output=True, text=True, check=False, env=env)
    if started.returncode != 0:
        raise SystemExit((started.stderr or started.stdout or "restart command failed")[-800:])
    print(json.dumps({
        "status": "scheduled",
        "host": host,
        "delay_seconds": delay_seconds,
        "reason": "authorized_pc24x7_physical_failover_failback_e2e",
        "observed_at_epoch": time.time(),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

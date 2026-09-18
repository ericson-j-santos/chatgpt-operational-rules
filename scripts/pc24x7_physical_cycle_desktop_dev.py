from __future__ import annotations

import json
import socket
import subprocess
import time

TARGET = "DESKTOP-PDQK954"


def main() -> int:
    host = socket.gethostname()
    if host.casefold() != TARGET.casefold():
        raise SystemExit(f"refusing physical cycle on unexpected host: {host}")
    delay_seconds = 10
    command = [
        "shutdown.exe",
        "/r",
        "/t",
        str(delay_seconds),
        "/d",
        "p:4:1",
        "/c",
        "Authorized PC24x7 physical failover/failback E2E",
    ]
    started = subprocess.run(command, capture_output=True, text=True, check=False)
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

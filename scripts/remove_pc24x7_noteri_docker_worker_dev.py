from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

STATE = Path.home() / "AppData" / "Local" / "ReqSys" / "TodoGlobal24x7Noteri" / "host-worker"
PID_FILE = STATE / "worker.pid"
CONTAINER = "todo-global-24x7-noteri-worker_noteri-1"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")


def host_worker_alive() -> tuple[bool, int]:
    if not PID_FILE.is_file():
        return False, 0
    try:
        pid = int(PID_FILE.read_text(encoding="ascii").strip())
    except ValueError:
        return False, 0
    result = run("tasklist", "/FI", f"PID eq {pid}", "/NH")
    return result.returncode == 0 and str(pid) in result.stdout, pid


def main() -> int:
    if socket.gethostname().casefold() != "noteri":
        raise SystemExit("cleanup must run on Noteri")
    alive, pid = host_worker_alive()
    if not alive:
        raise SystemExit("Noteri host worker is not alive; refusing Docker fallback removal")
    before = run("docker", "ps", "-a", "--filter", f"name={CONTAINER}", "--format", "{{.Names}}")
    if before.returncode != 0:
        raise SystemExit("docker inventory failed")
    existed = CONTAINER in before.stdout.splitlines()
    if existed:
        removed = run("docker", "rm", "-f", CONTAINER)
        if removed.returncode != 0:
            raise SystemExit("Docker fallback removal failed")
    after = run("docker", "ps", "-a", "--filter", f"name={CONTAINER}", "--format", "{{.Names}}")
    if after.returncode != 0 or CONTAINER in after.stdout.splitlines():
        raise SystemExit("Docker fallback still present")
    print(json.dumps({"status": "ok", "docker_fallback_removed": existed, "host_worker_alive": True, "host_worker_pid": pid}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

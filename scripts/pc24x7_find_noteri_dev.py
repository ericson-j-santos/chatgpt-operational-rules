from __future__ import annotations

import json
import socket
import subprocess

NAMES = ("Noteri", "Noteri.local")


def run(args: list[str], timeout: int = 10) -> dict[str, object]:
    try:
        cp = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return {
            "rc": cp.returncode,
            "stdout_tail": cp.stdout[-4000:],
            "stderr_tail": cp.stderr[-1000:],
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    resolved: dict[str, object] = {}
    for name in NAMES:
        try:
            host, aliases, addrs = socket.gethostbyname_ex(name)
            resolved[name] = {"host": host, "aliases": aliases, "addresses": addrs}
        except Exception as exc:
            resolved[name] = {"error": f"{type(exc).__name__}: {exc}"}

    result = {
        "resolved": resolved,
        "ping_name": run(["ping.exe", "-4", "-n", "1", "-w", "1500", "Noteri"]),
        "nbtstat_name": run(["nbtstat.exe", "-a", "Noteri"]),
        "arp": run(["arp.exe", "-a"]),
    }
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

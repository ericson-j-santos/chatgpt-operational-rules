from __future__ import annotations

import ipaddress
import json
import re
import socket
import subprocess
import time

NETWORK = ipaddress.ip_network("192.168.1.0/24")
TARGET_MAC = "78-46-5c-1a-78-bb"


def populate_arp() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.05)
    try:
        for host in NETWORK.hosts():
            try:
                sock.sendto(b"\x00", (str(host), 9))
            except OSError:
                pass
        time.sleep(2)
    finally:
        sock.close()


def main() -> int:
    populate_arp()
    cp = subprocess.run(
        ["arp.exe", "-a"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    matches = []
    pattern = re.compile(r"\b(192\.168\.1\.\d+)\s+([0-9a-fA-F-]{17})\b")
    for line in cp.stdout.splitlines():
        m = pattern.search(line)
        if not m:
            continue
        ip, mac = m.group(1), m.group(2).lower()
        if mac == TARGET_MAC:
            matches.append({"ip": ip, "mac": mac, "line": line.strip()})
    print(json.dumps({"target_mac": TARGET_MAC, "matches": matches}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

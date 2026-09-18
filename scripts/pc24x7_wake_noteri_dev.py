from __future__ import annotations

import json
import socket
import time

MAC = "78:46:5c:1a:78:bb"
BROADCASTS = ("192.168.1.255", "255.255.255.255")
PORTS = (7, 9)
BURSTS = 5


def magic_packet(mac: str) -> bytes:
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    if len(raw) != 6:
        raise ValueError("invalid MAC length")
    return b"\xff" * 6 + raw * 16


def main() -> int:
    packet = magic_packet(MAC)
    sent: list[str] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        for _ in range(BURSTS):
            for address in BROADCASTS:
                for port in PORTS:
                    sock.sendto(packet, (address, port))
                    sent.append(f"{address}:{port}")
                    time.sleep(0.1)
    finally:
        sock.close()
    print(json.dumps({"status": "sent", "mac": MAC, "targets": sent, "bursts": BURSTS}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

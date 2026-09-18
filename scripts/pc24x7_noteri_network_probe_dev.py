from __future__ import annotations

import json
import socket

HOST = "192.168.1.60"
PORTS = (135, 445, 3389, 5985, 5986, 22)


def main() -> int:
    result: dict[str, str] = {}
    for port in PORTS:
        sock = socket.socket()
        sock.settimeout(1.5)
        try:
            sock.connect((HOST, port))
            result[str(port)] = "open"
        except Exception as exc:
            result[str(port)] = type(exc).__name__
        finally:
            sock.close()
    print(json.dumps({"host": HOST, "ports": result, "secret_value_exposed": False}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

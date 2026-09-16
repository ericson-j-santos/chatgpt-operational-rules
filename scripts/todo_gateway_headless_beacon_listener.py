#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
from datetime import UTC, datetime
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=19194)
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((args.host, args.port))
        server.listen(1)
        server.settimeout(args.timeout)
        conn, addr = server.accept()
        with conn:
            conn.settimeout(10)
            data = conn.recv(65536).decode("utf-8", errors="replace")

    record = {"received_at": datetime.now(UTC).isoformat(), "from": addr[0], "payload": data}
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(record, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

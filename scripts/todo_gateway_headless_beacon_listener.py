#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
import time
from datetime import UTC, datetime
from pathlib import Path


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def persist(out: Path, records: list[dict], *, terminal_received: bool, timed_out: bool) -> None:
    payload = {
        "contract": "todo-global-headless-beacon-listener-v2",
        "updated_at": now_iso(),
        "terminal_received": terminal_received,
        "timed_out": timed_out,
        "records": records,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=19194)
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--max-events", type=int, default=32)
    args = parser.parse_args()
    if args.timeout < 1 or args.max_events < 1:
        parser.error("timeout and max-events must be positive")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    terminal_received = False
    deadline = time.monotonic() + args.timeout

    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((args.host, args.port))
        server.listen(5)
        while len(records) < args.max_events:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            server.settimeout(min(5.0, remaining))
            try:
                conn, addr = server.accept()
            except TimeoutError:
                continue
            with conn:
                conn.settimeout(10)
                data = conn.recv(65536).decode("utf-8", errors="replace")
            try:
                decoded = json.loads(data)
            except json.JSONDecodeError:
                decoded = None
            record = {
                "received_at": now_iso(),
                "from": addr[0],
                "payload": data,
                "decoded": decoded,
            }
            records.append(record)
            terminal_received = bool(isinstance(decoded, dict) and decoded.get("terminal") is True)
            persist(out, records, terminal_received=terminal_received, timed_out=False)
            print(json.dumps(record, ensure_ascii=True), flush=True)
            if terminal_received:
                return 0

    persist(out, records, terminal_received=False, timed_out=True)
    summary = {"terminal_received": False, "timed_out": True, "records": len(records)}
    print(json.dumps(summary), flush=True)
    return 2 if records else 3


if __name__ == "__main__":
    raise SystemExit(main())

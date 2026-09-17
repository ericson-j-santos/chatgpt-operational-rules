from __future__ import annotations

import os
import selectors
import socket
import threading

LISTEN_HOST = os.environ.get("DB_BRIDGE_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("DB_BRIDGE_LISTEN_PORT", "15432"))
UPSTREAM_HOST = os.environ.get("DB_BRIDGE_UPSTREAM_HOST", "db")
UPSTREAM_PORT = int(os.environ.get("DB_BRIDGE_UPSTREAM_PORT", "5432"))
ALLOWED = {x.strip() for x in os.environ.get("DB_BRIDGE_ALLOWED_REMOTE_IPS", "192.168.1.60,127.0.0.1").split(",") if x.strip()}


def proxy(client: socket.socket, peer_ip: str) -> None:
    if peer_ip not in ALLOWED:
        print(f"bridge_rejected peer={peer_ip}", flush=True)
        client.close()
        return
    upstream = socket.create_connection((UPSTREAM_HOST, UPSTREAM_PORT), timeout=5)
    client.setblocking(False)
    upstream.setblocking(False)
    sel = selectors.DefaultSelector()
    sel.register(client, selectors.EVENT_READ, upstream)
    sel.register(upstream, selectors.EVENT_READ, client)
    print(f"bridge_accepted peer={peer_ip}", flush=True)
    try:
        while True:
            events = sel.select(timeout=30)
            if not events:
                continue
            for key, _ in events:
                data = key.fileobj.recv(65536)
                if not data:
                    return
                key.data.sendall(data)
    finally:
        sel.close()
        client.close()
        upstream.close()


def main() -> int:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((LISTEN_HOST, LISTEN_PORT))
    server.listen(64)
    print(f"bridge_ready port={LISTEN_PORT} allowed={','.join(sorted(ALLOWED))}", flush=True)
    while True:
        client, addr = server.accept()
        threading.Thread(target=proxy, args=(client, addr[0]), daemon=True).start()


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Read-only reachability probe for an authorized Windows host.

Only DNS resolution and TCP connect are performed. No application data,
credentials or response bodies are read.
"""
from __future__ import annotations
import argparse, json, socket, time

SAFE_HOSTS = {"DESKTOP-PDQK954"}
SAFE_PORTS = {8210, 18097}


def probe(host: str, port: int, timeout: float) -> dict:
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"port": port, "reachable": True, "error": None, "duration_ms": round((time.monotonic()-started)*1000)}
    except OSError as exc:
        return {"port": port, "reachable": False, "error": type(exc).__name__, "duration_ms": round((time.monotonic()-started)*1000)}


def main() -> int:
    p=argparse.ArgumentParser()
    p.add_argument("--host", required=True)
    p.add_argument("--ports", nargs="+", type=int, required=True)
    p.add_argument("--timeout", type=float, default=2.0)
    ns=p.parse_args()
    if ns.host not in SAFE_HOSTS:
        raise SystemExit("host_not_allowlisted")
    if any(port not in SAFE_PORTS for port in ns.ports):
        raise SystemExit("port_not_allowlisted")
    try:
        infos=socket.getaddrinfo(ns.host, None, family=socket.AF_INET)
        ips=sorted({item[4][0] for item in infos})
    except OSError as exc:
        print(json.dumps({"host":ns.host,"dns_ok":False,"dns_error":type(exc).__name__,"ports":[],"secret_values_exposed":False},sort_keys=True))
        return 2
    payload={"host":ns.host,"dns_ok":True,"resolved_ipv4":ips,"ports":[probe(ns.host,p,ns.timeout) for p in ns.ports],"secret_values_exposed":False}
    print(json.dumps(payload,sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())

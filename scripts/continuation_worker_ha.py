#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time

from scripts.continuation_worker import PostgresContinuationQueue, process_batch
from scripts.host_router import DynamicNodeHealth, HostRouter, PostgresNodeHealthStore
from scripts.ha_database_guard import validate_ha_database


def bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    value = int(raw)
    if not minimum <= value <= maximum:
        raise SystemExit(f"{name} out of range")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Worker HA de continuações PC24x7")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=5)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--lease-seconds", type=int, default=120)
    parser.add_argument("--worker-id", default=os.environ.get("WORKER_ID", "continuation-worker-pc24x7"))
    args = parser.parse_args()
    if not 1 <= args.limit <= 100 or not 1 <= args.lease_seconds <= 3600:
        raise SystemExit("invalid worker limits")

    validate_ha_database(os.environ)
    dsn = os.environ.get("DATABASE_URL", "").strip()
    primary = os.environ.get("ROUTER_PRIMARY_NODE", "DESKTOP-PDQK954").strip()
    secondary = os.environ.get("ROUTER_SECONDARY_NODE", "Noteri").strip()
    node_id = os.environ.get("WORKER_NODE_ID", primary).strip()
    ttl_seconds = bounded_int("ROUTER_HEARTBEAT_TTL_SECONDS", 20, 5, 300)
    stability_seconds = bounded_int("ROUTER_PRIMARY_STABILITY_SECONDS", 15, 0, 300)
    capabilities = frozenset(
        x.strip() for x in os.environ.get("WORKER_CAPABILITIES", "continuation").split(",") if x.strip()
    )

    queue = PostgresContinuationQueue(dsn)
    health_store = PostgresNodeHealthStore(dsn)
    router = HostRouter(primary, secondary)
    nodes = DynamicNodeHealth(health_store, primary, secondary, ttl_seconds, stability_seconds)

    while True:
        health_store.touch(node_id, args.worker_id, ttl_seconds, capabilities)
        result = process_batch(
            queue,
            limit=args.limit,
            lease_seconds=args.lease_seconds,
            router=router,
            node_id=node_id,
            nodes=nodes,
        )
        queue.heartbeat(args.worker_id, result)
        print(json.dumps({
            **result,
            "node_id": node_id,
            "heartbeat_ttl_seconds": ttl_seconds,
            "primary_stability_seconds": stability_seconds,
        }, sort_keys=True), flush=True)
        if args.once:
            return 0
        time.sleep(max(1, min(args.interval_seconds, 300)))


if __name__ == "__main__":
    raise SystemExit(main())

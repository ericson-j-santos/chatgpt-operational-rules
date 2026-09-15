from __future__ import annotations

import argparse
import logging
import os
import time
from typing import Any

from services.todo_gateway.notion_sink import (
    NotionTodoSink,
    PermanentSinkError,
    TransientSinkError,
)
from services.todo_gateway.repository import PostgresQueueRepository

LOGGER = logging.getLogger("todo_worker")


def process_once(
    queue: Any,
    sink: Any,
    *,
    limit: int = 10,
    lease_seconds: int = 120,
    max_attempts: int = 3,
    backoff_seconds: int = 30,
) -> dict[str, int]:
    reserved = queue.reserve(limit=limit, lease_seconds=lease_seconds)
    result = {"reserved": len(reserved), "processed": 0, "retried": 0, "dlq": 0}
    for item in reserved:
        try:
            with queue.idempotency_lock(item.event.idempotency_key):
                sink.upsert(item.event)
                if not queue.ack(item.event.event_id):
                    raise TransientSinkError(f"ack failed for {item.event.event_id}")
        except PermanentSinkError as exc:
            target = queue.fail(
                item.event.event_id,
                str(exc),
                max_attempts=1,
                backoff_seconds=0,
            )
            result["dlq" if target == "DLQ" else "retried"] += 1
        except Exception as exc:
            target = queue.fail(
                item.event.event_id,
                str(exc),
                max_attempts=max_attempts,
                backoff_seconds=backoff_seconds,
            )
            result["dlq" if target == "DLQ" else "retried"] += 1
        else:
            result["processed"] += 1
    return result


def build_from_env():
    database_url = os.environ.get("DATABASE_URL", "")
    notion_token = os.environ.get("NOTION_TOKEN", "")
    data_source_id = os.environ.get("NOTION_DATA_SOURCE_ID", "")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    if not notion_token or not data_source_id:
        raise RuntimeError("NOTION_TOKEN and NOTION_DATA_SOURCE_ID are required")
    return (
        PostgresQueueRepository(database_url),
        NotionTodoSink(notion_token, data_source_id),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

    queue, sink = build_from_env()
    while True:
        result = process_once(queue, sink)
        LOGGER.info("worker iteration %s", result)
        if args.once:
            return 0
        if result["reserved"] == 0:
            time.sleep(max(0.2, args.poll_seconds))


if __name__ == "__main__":
    raise SystemExit(main())

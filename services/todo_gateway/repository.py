from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from scripts.todo_event_bus import TodoEvent


@dataclass(frozen=True)
class ReservedEvent:
    event: TodoEvent
    attempts: int


class PostgresQueueRepository:
    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError("dsn is required")
        self.dsn = dsn

    def _connect(self):
        import psycopg

        return psycopg.connect(self.dsn)

    def ready(self) -> bool:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            return cur.fetchone()[0] == 1

    def enqueue(self, event: TodoEvent) -> bool:
        payload = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO todo_bus.queue_events(
                    event_id, schema_version, event_type, idempotency_key,
                    correlation_id, project, producer, payload
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT (event_id) DO NOTHING
                RETURNING event_id
                """,
                (
                    event.event_id,
                    event.schema_version,
                    event.event_type,
                    event.idempotency_key,
                    event.correlation_id,
                    event.project,
                    event.producer,
                    payload,
                ),
            )
            inserted = cur.fetchone() is not None
        return inserted

    def reserve(self, limit: int = 10, lease_seconds: int = 120) -> list[ReservedEvent]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT event_id,payload,attempts FROM todo_bus.reserve_events(%s,%s)",
                (limit, lease_seconds),
            )
            rows = cur.fetchall()
        result: list[ReservedEvent] = []
        for _, payload, attempts in rows:
            if isinstance(payload, str):
                payload = json.loads(payload)
            result.append(ReservedEvent(TodoEvent.from_dict(payload), int(attempts)))
        return result

    @contextmanager
    def idempotency_lock(self, key: str) -> Iterator[None]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_lock(hashtextextended(%s,0))", (key,))
            yield
        finally:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (key,))
            finally:
                conn.close()

    def ack(self, event_id: str) -> bool:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT todo_bus.ack_event(%s)", (event_id,))
            return bool(cur.fetchone()[0])

    def fail(
        self,
        event_id: str,
        error: str,
        *,
        max_attempts: int = 3,
        backoff_seconds: int = 30,
    ) -> str:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT todo_bus.fail_event(%s,%s,%s,%s)",
                (event_id, error, max_attempts, backoff_seconds),
            )
            return str(cur.fetchone()[0])

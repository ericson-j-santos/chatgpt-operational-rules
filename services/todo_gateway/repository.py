from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

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

    @staticmethod
    def _iso(value: Any) -> str | None:
        if value is None:
            return None
        isoformat = getattr(value, "isoformat", None)
        return isoformat() if callable(isoformat) else str(value)

    @staticmethod
    def _payload_dict(payload: Any) -> dict[str, Any]:
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise ValueError("stored TodoEvent payload must be an object")
        return payload

    @classmethod
    def _todo_row(cls, row: tuple[Any, ...]) -> dict[str, Any]:
        (
            event_id,
            idempotency_key,
            correlation_id,
            project,
            producer,
            payload,
            state,
            attempts,
            last_error,
            created_at,
            updated_at,
        ) = row
        event_payload = cls._payload_dict(payload)
        return {
            "event_id": str(event_id),
            "idempotency_key": str(idempotency_key),
            "correlation_id": str(correlation_id),
            "project": str(project),
            "producer": producer,
            "queue_state": str(state),
            "attempts": int(attempts),
            "last_error": last_error,
            "created_at": cls._iso(created_at),
            "updated_at": cls._iso(updated_at),
            "todo": dict(event_payload.get("todo") or {}),
        }

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

    def list_latest_todos(self, limit: int = 100) -> list[dict[str, Any]]:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                WITH latest AS (
                    SELECT DISTINCT ON (idempotency_key)
                        event_id, idempotency_key, correlation_id, project, producer,
                        payload, state, attempts, last_error, created_at, updated_at
                    FROM todo_bus.queue_events
                    ORDER BY idempotency_key, created_at DESC, event_id DESC
                )
                SELECT
                    event_id, idempotency_key, correlation_id, project, producer,
                    payload, state, attempts, last_error, created_at, updated_at
                FROM latest
                ORDER BY updated_at DESC, idempotency_key
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        return [self._todo_row(row) for row in rows]

    def get_latest_todo(self, idempotency_key: str) -> dict[str, Any] | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    event_id, idempotency_key, correlation_id, project, producer,
                    payload, state, attempts, last_error, created_at, updated_at
                FROM todo_bus.queue_events
                WHERE idempotency_key = %s
                ORDER BY created_at DESC, event_id DESC
                LIMIT 1
                """,
                (idempotency_key,),
            )
            row = cur.fetchone()
        return self._todo_row(row) if row else None

    def request_continuation(
        self,
        idempotency_key: str,
        correlation_id: str,
    ) -> dict[str, Any] | None:
        if len(correlation_id) < 8 or len(correlation_id) > 128:
            raise ValueError("correlation_id must have 8..128 characters")

        latest = self.get_latest_todo(idempotency_key)
        if latest is None:
            return None
        status = str(latest["todo"].get("status") or "")
        if status in {"CONCLUÍDO", "CANCELADO"}:
            raise ValueError(f"terminal TODO cannot be continued: {status}")

        request_id = f"cont-{uuid.uuid4().hex}"
        payload = json.dumps(latest["todo"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO todo_bus.continuation_requests(
                    request_id, idempotency_key, basis_event_id, correlation_id,
                    project, todo_payload
                )
                VALUES (%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT (idempotency_key, basis_event_id)
                    WHERE state IN ('PENDING','PROCESSING','HUMAN_GATE')
                DO UPDATE SET updated_at = todo_bus.continuation_requests.updated_at
                RETURNING request_id, idempotency_key, basis_event_id, correlation_id,
                          project, state, attempts, last_error, created_at, updated_at,
                          (xmax <> 0) AS duplicate
                """,
                (
                    request_id,
                    idempotency_key,
                    latest["event_id"],
                    correlation_id,
                    latest["project"],
                    payload,
                ),
            )
            row = cur.fetchone()
        return {
            "request_id": str(row[0]),
            "idempotency_key": str(row[1]),
            "basis_event_id": str(row[2]),
            "correlation_id": str(row[3]),
            "project": str(row[4]),
            "state": str(row[5]),
            "attempts": int(row[6]),
            "last_error": row[7],
            "created_at": self._iso(row[8]),
            "updated_at": self._iso(row[9]),
            "duplicate": bool(row[10]),
        }

    def list_continuation_requests(self, limit: int = 100) -> list[dict[str, Any]]:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT request_id, idempotency_key, basis_event_id, correlation_id,
                       project, state, attempts, last_error, created_at, updated_at
                FROM todo_bus.continuation_requests
                ORDER BY created_at DESC, request_id
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        return [
            {
                "request_id": str(row[0]),
                "idempotency_key": str(row[1]),
                "basis_event_id": str(row[2]),
                "correlation_id": str(row[3]),
                "project": str(row[4]),
                "state": str(row[5]),
                "attempts": int(row[6]),
                "last_error": row[7],
                "created_at": self._iso(row[8]),
                "updated_at": self._iso(row[9]),
            }
            for row in rows
        ]

    @staticmethod
    def _front_row(row: tuple[Any, ...]) -> dict[str, Any]:
        blockers = row[13]
        if isinstance(blockers, str):
            blockers = json.loads(blockers)
        return {
            "front_id": str(row[0]),
            "title": str(row[1]),
            "state": str(row[2]),
            "source_of_truth": str(row[3]),
            "source_ref": str(row[4]),
            "source_updated_at": PostgresQueueRepository._iso(row[5]),
            "current_event_id": str(row[6]),
            "correlation_id": str(row[7]),
            "last_sha": row[8],
            "last_pr": row[9],
            "last_issue": row[10],
            "last_run": row[11],
            "next_step": row[12],
            "blockers": list(blockers or []),
            "summary": row[14],
            "created_at": PostgresQueueRepository._iso(row[15]),
            "updated_at": PostgresQueueRepository._iso(row[16]),
        }

    def record_operational_front(
        self,
        front_id: str,
        snapshot: dict[str, Any],
        correlation_id: str,
    ) -> dict[str, Any]:
        front_id = str(front_id or "").strip()
        if len(front_id) < 3 or len(front_id) > 128:
            raise ValueError("front_id must have 3..128 characters")
        if len(correlation_id) < 8 or len(correlation_id) > 128:
            raise ValueError("correlation_id must have 8..128 characters")
        required = ("title", "state", "source_of_truth", "source_ref", "source_event_id", "source_updated_at")
        missing = [name for name in required if not str(snapshot.get(name) or "").strip()]
        if missing:
            raise ValueError("missing front fields: " + ", ".join(missing))
        blockers = snapshot.get("blockers", [])
        if not isinstance(blockers, list):
            raise ValueError("blockers must be an array")

        source_of_truth = str(snapshot["source_of_truth"]).strip()
        source_event_id = str(snapshot["source_event_id"]).strip()
        digest = hashlib.sha256(
            f"{front_id}\0{source_of_truth}\0{source_event_id}".encode("utf-8")
        ).hexdigest()
        event_id = f"front-{digest[:24]}"
        canonical = {
            "front_id": front_id,
            "title": str(snapshot["title"]).strip(),
            "state": str(snapshot["state"]).strip(),
            "source_of_truth": source_of_truth,
            "source_ref": str(snapshot["source_ref"]).strip(),
            "source_event_id": source_event_id,
            "source_updated_at": str(snapshot["source_updated_at"]).strip(),
            "last_sha": str(snapshot.get("last_sha") or "").strip() or None,
            "last_pr": str(snapshot.get("last_pr") or "").strip() or None,
            "last_issue": str(snapshot.get("last_issue") or "").strip() or None,
            "last_run": str(snapshot.get("last_run") or "").strip() or None,
            "next_step": str(snapshot.get("next_step") or "").strip() or None,
            "blockers": blockers,
            "summary": str(snapshot.get("summary") or "").strip() or None,
        }
        payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        blockers_json = json.dumps(blockers, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO todo_bus.operational_front_history(
                    event_id, front_id, source_of_truth, source_event_id,
                    idempotency_key, correlation_id, source_updated_at, snapshot
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s::timestamptz,%s::jsonb)
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING event_id
                """,
                (event_id, front_id, source_of_truth, source_event_id, digest,
                 correlation_id, canonical["source_updated_at"], payload),
            )
            history_inserted = cur.fetchone() is not None
            row = None
            if history_inserted:
                cur.execute(
                    """
                    INSERT INTO todo_bus.operational_fronts(
                        front_id, title, state, source_of_truth, source_ref,
                        source_updated_at, current_event_id, correlation_id,
                        last_sha, last_pr, last_issue, last_run, next_step, blockers, summary
                    )
                    VALUES (%s,%s,%s,%s,%s,%s::timestamptz,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                    ON CONFLICT (front_id) DO UPDATE SET
                        title=EXCLUDED.title, state=EXCLUDED.state,
                        source_of_truth=EXCLUDED.source_of_truth, source_ref=EXCLUDED.source_ref,
                        source_updated_at=EXCLUDED.source_updated_at,
                        current_event_id=EXCLUDED.current_event_id,
                        correlation_id=EXCLUDED.correlation_id,
                        last_sha=EXCLUDED.last_sha, last_pr=EXCLUDED.last_pr,
                        last_issue=EXCLUDED.last_issue, last_run=EXCLUDED.last_run,
                        next_step=EXCLUDED.next_step, blockers=EXCLUDED.blockers,
                        summary=EXCLUDED.summary, updated_at=clock_timestamp()
                    WHERE EXCLUDED.source_updated_at > todo_bus.operational_fronts.source_updated_at
                    RETURNING front_id,title,state,source_of_truth,source_ref,source_updated_at,
                              current_event_id,correlation_id,last_sha,last_pr,last_issue,last_run,
                              next_step,blockers,summary,created_at,updated_at
                    """,
                    (front_id, canonical["title"], canonical["state"], source_of_truth,
                     canonical["source_ref"], canonical["source_updated_at"], event_id,
                     correlation_id, canonical["last_sha"], canonical["last_pr"],
                     canonical["last_issue"], canonical["last_run"], canonical["next_step"],
                     blockers_json, canonical["summary"]),
                )
                row = cur.fetchone()
            if row is None:
                cur.execute(
                    """
                    SELECT front_id,title,state,source_of_truth,source_ref,source_updated_at,
                           current_event_id,correlation_id,last_sha,last_pr,last_issue,last_run,
                           next_step,blockers,summary,created_at,updated_at
                    FROM todo_bus.operational_fronts WHERE front_id=%s
                    """,
                    (front_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise RuntimeError("front history exists without current front")
                incoming = canonical["source_updated_at"]
                existing = self._iso(row[5])
                outcome = "DUPLICATE" if not history_inserted else ("CONFLICT" if incoming == existing else "STALE")
            else:
                outcome = "APPLIED"
        result = self._front_row(row)
        result["outcome"] = outcome
        result["duplicate"] = outcome == "DUPLICATE"
        return result

    def list_operational_fronts(self, limit: int = 100) -> list[dict[str, Any]]:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT front_id,title,state,source_of_truth,source_ref,source_updated_at,
                       current_event_id,correlation_id,last_sha,last_pr,last_issue,last_run,
                       next_step,blockers,summary,created_at,updated_at
                FROM todo_bus.operational_fronts
                ORDER BY source_updated_at DESC, front_id
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        return [self._front_row(row) for row in rows]

    def list_operational_front_history(self, front_id: str, limit: int = 100) -> list[dict[str, Any]]:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT event_id,source_of_truth,source_event_id,idempotency_key,
                       correlation_id,source_updated_at,snapshot,recorded_at
                FROM todo_bus.operational_front_history
                WHERE front_id=%s
                ORDER BY source_updated_at DESC, recorded_at DESC
                LIMIT %s
                """,
                (front_id, limit),
            )
            rows = cur.fetchall()
        return [
            {
                "event_id": str(row[0]),
                "source_of_truth": str(row[1]),
                "source_event_id": str(row[2]),
                "idempotency_key": str(row[3]),
                "correlation_id": str(row[4]),
                "source_updated_at": self._iso(row[5]),
                "snapshot": self._payload_dict(row[6]),
                "recorded_at": self._iso(row[7]),
            }
            for row in rows
        ]

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

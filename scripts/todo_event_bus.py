#!/usr/bin/env python3
"""Núcleo de referência para TodoEvent v1: fila SQLite durável e upsert idempotente."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

VALID_EVENT_TYPES = {
    "todo.created",
    "todo.updated",
    "todo.status.changed",
    "todo.evidence.updated",
    "todo.reconcile.requested",
}
VALID_STATUSES = {"PENDENTE", "EM ANDAMENTO", "BLOQUEADO", "CONCLUÍDO", "CANCELADO"}
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


def utc_now_ts() -> float:
    return time.time()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_idempotency_key(project: str, item_type: str, external_id: str) -> str:
    raw = f"{project.strip().lower()}|{item_type.strip().lower()}|{external_id.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_event_dict(data: dict[str, Any]) -> None:
    required = (
        "schema_version",
        "event_id",
        "event_type",
        "occurred_at",
        "correlation_id",
        "idempotency_key",
        "project",
        "todo",
    )
    missing = [key for key in required if key not in data]
    if missing:
        raise ValueError("campos obrigatórios ausentes: " + ", ".join(missing))
    if data["schema_version"] != "1.0":
        raise ValueError("schema_version deve ser 1.0")
    if not isinstance(data["event_id"], str) or not (8 <= len(data["event_id"]) <= 128):
        raise ValueError("event_id inválido")
    if data["event_type"] not in VALID_EVENT_TYPES:
        raise ValueError("event_type inválido")
    if not isinstance(data["correlation_id"], str) or not (8 <= len(data["correlation_id"]) <= 128):
        raise ValueError("correlation_id inválido")
    if not isinstance(data["idempotency_key"], str) or not SHA256_RE.fullmatch(data["idempotency_key"]):
        raise ValueError("idempotency_key deve ser SHA-256 hexadecimal minúsculo")
    if not isinstance(data["project"], str) or not data["project"].strip():
        raise ValueError("project inválido")
    try:
        parsed = datetime.fromisoformat(str(data["occurred_at"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("occurred_at inválido") from exc
    if parsed.tzinfo is None:
        raise ValueError("occurred_at deve conter timezone")
    todo = data["todo"]
    if not isinstance(todo, dict):
        raise ValueError("todo deve ser objeto")
    for field in ("title", "type", "status"):
        if field not in todo:
            raise ValueError(f"todo.{field} ausente")
    if not isinstance(todo["title"], str) or not todo["title"].strip():
        raise ValueError("todo.title inválido")
    if not isinstance(todo["type"], str) or not todo["type"].strip():
        raise ValueError("todo.type inválido")
    if todo["status"] not in VALID_STATUSES:
        raise ValueError("todo.status inválido")
    if todo["status"] == "CONCLUÍDO":
        if not str(todo.get("completion_criteria") or "").strip():
            raise ValueError("CONCLUÍDO exige completion_criteria")
        if not str(todo.get("evidence") or "").strip():
            raise ValueError("CONCLUÍDO exige evidence")
    if todo["status"] == "BLOQUEADO":
        if not str(todo.get("blocker") or "").strip():
            raise ValueError("BLOQUEADO exige blocker")
        if not str(todo.get("next_action") or "").strip():
            raise ValueError("BLOQUEADO exige next_action")


@dataclass(frozen=True)
class TodoEvent:
    schema_version: str
    event_id: str
    event_type: str
    occurred_at: str
    correlation_id: str
    idempotency_key: str
    project: str
    todo: dict[str, Any]
    producer: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TodoEvent":
        validate_event_dict(data)
        return cls(
            schema_version=data["schema_version"],
            event_id=data["event_id"],
            event_type=data["event_type"],
            occurred_at=data["occurred_at"],
            correlation_id=data["correlation_id"],
            idempotency_key=data["idempotency_key"],
            project=data["project"],
            todo=dict(data["todo"]),
            producer=data.get("producer"),
        )

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "correlation_id": self.correlation_id,
            "idempotency_key": self.idempotency_key,
            "project": self.project,
            "todo": self.todo,
        }
        if self.producer:
            result["producer"] = self.producer
        return result


@dataclass(frozen=True)
class ReservedEvent:
    event: TodoEvent
    attempts: int


class TodoSink(Protocol):
    def upsert(self, event: TodoEvent) -> str:
        """Persiste/atualiza um TODO lógico e retorna seu identificador."""


class DurableSQLiteQueue:
    def __init__(self, path: str | Path, *, max_attempts: int = 3) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts deve ser >= 1")
        self.path = str(path)
        self.max_attempts = max_attempts
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS queue_events (
                    event_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('PENDING','PROCESSING','PROCESSED','DLQ')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL,
                    lease_until REAL,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_queue_ready ON queue_events(state, available_at, lease_until)"
            )

    def enqueue(self, event: TodoEvent, *, now: float | None = None) -> bool:
        validate_event_dict(event.to_dict())
        ts = utc_now_ts() if now is None else now
        payload = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO queue_events(
                    event_id,idempotency_key,correlation_id,payload_json,state,
                    attempts,available_at,lease_until,last_error,created_at,updated_at
                ) VALUES(?,?,?,?, 'PENDING', 0, ?, NULL, NULL, ?, ?)
                """,
                (event.event_id, event.idempotency_key, event.correlation_id, payload, ts, ts, ts),
            )
            return cur.rowcount == 1

    def recover_expired_leases(self, *, now: float | None = None) -> int:
        ts = utc_now_ts() if now is None else now
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE queue_events
                   SET state='PENDING', lease_until=NULL, updated_at=?
                 WHERE state='PROCESSING' AND lease_until IS NOT NULL AND lease_until <= ?
                """,
                (ts, ts),
            )
            return cur.rowcount

    def reserve(
        self,
        *,
        limit: int = 10,
        lease_seconds: int = 60,
        now: float | None = None,
    ) -> list[ReservedEvent]:
        if limit < 1:
            return []
        if lease_seconds < 1:
            raise ValueError("lease_seconds deve ser >= 1")
        ts = utc_now_ts() if now is None else now
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE queue_events
                   SET state='PENDING', lease_until=NULL, updated_at=?
                 WHERE state='PROCESSING' AND lease_until IS NOT NULL AND lease_until <= ?
                """,
                (ts, ts),
            )
            rows = conn.execute(
                """
                SELECT event_id
                  FROM queue_events
                 WHERE state='PENDING' AND available_at <= ?
                 ORDER BY created_at, event_id
                 LIMIT ?
                """,
                (ts, limit),
            ).fetchall()
            ids = [row["event_id"] for row in rows]
            if not ids:
                conn.execute("COMMIT")
                return []
            placeholders = ",".join("?" for _ in ids)
            lease_until = ts + lease_seconds
            conn.execute(
                f"""
                UPDATE queue_events
                   SET state='PROCESSING',
                       attempts=attempts+1,
                       lease_until=?,
                       updated_at=?
                 WHERE event_id IN ({placeholders}) AND state='PENDING'
                """,
                (lease_until, ts, *ids),
            )
            reserved_rows = conn.execute(
                f"""
                SELECT payload_json, attempts
                  FROM queue_events
                 WHERE event_id IN ({placeholders}) AND state='PROCESSING'
                 ORDER BY created_at, event_id
                """,
                ids,
            ).fetchall()
            conn.execute("COMMIT")
            return [
                ReservedEvent(
                    event=TodoEvent.from_dict(json.loads(row["payload_json"])),
                    attempts=int(row["attempts"]),
                )
                for row in reserved_rows
            ]
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def ack(self, event_id: str, *, now: float | None = None) -> None:
        ts = utc_now_ts() if now is None else now
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE queue_events
                   SET state='PROCESSED', lease_until=NULL, last_error=NULL, updated_at=?
                 WHERE event_id=? AND state='PROCESSING'
                """,
                (ts, event_id),
            )
            if cur.rowcount != 1:
                raise ValueError(f"evento não reservado: {event_id}")

    def fail(
        self,
        event_id: str,
        error: str,
        *,
        backoff_seconds: float = 0,
        now: float | None = None,
    ) -> str:
        ts = utc_now_ts() if now is None else now
        safe_error = str(error).replace("\n", " ")[:1000]
        with self._connect() as conn:
            row = conn.execute(
                "SELECT attempts,state FROM queue_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if row is None or row["state"] != "PROCESSING":
                raise ValueError(f"evento não reservado: {event_id}")
            target = "DLQ" if int(row["attempts"]) >= self.max_attempts else "PENDING"
            available_at = ts if target == "DLQ" else ts + max(0.0, backoff_seconds)
            conn.execute(
                """
                UPDATE queue_events
                   SET state=?, available_at=?, lease_until=NULL, last_error=?, updated_at=?
                 WHERE event_id=?
                """,
                (target, available_at, safe_error, ts, event_id),
            )
            return target

    def state(self, event_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT event_id,idempotency_key,correlation_id,state,attempts,
                       available_at,lease_until,last_error,created_at,updated_at
                  FROM queue_events WHERE event_id=?
                """,
                (event_id,),
            ).fetchone()
            return dict(row) if row else None

    def counts(self) -> dict[str, int]:
        result = {"PENDING": 0, "PROCESSING": 0, "PROCESSED": 0, "DLQ": 0}
        with self._connect() as conn:
            for row in conn.execute("SELECT state,COUNT(*) AS n FROM queue_events GROUP BY state"):
                result[row["state"]] = int(row["n"])
        return result


class SQLiteTodoSink:
    """Sink de referência para provar o contrato idempotente sem depender de serviço externo."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS todos (
                    idempotency_key TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    external_id TEXT,
                    title TEXT NOT NULL,
                    item_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority TEXT,
                    correlation_id TEXT NOT NULL,
                    last_event_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    update_count INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def upsert(self, event: TodoEvent) -> str:
        validate_event_dict(event.to_dict())
        todo = event.todo
        now = utc_now_ts()
        payload = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO todos(
                    idempotency_key,project,external_id,title,item_type,status,priority,
                    correlation_id,last_event_id,payload_json,update_count,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?)
                ON CONFLICT(idempotency_key) DO UPDATE SET
                    project=excluded.project,
                    external_id=excluded.external_id,
                    title=excluded.title,
                    item_type=excluded.item_type,
                    status=excluded.status,
                    priority=excluded.priority,
                    correlation_id=excluded.correlation_id,
                    last_event_id=excluded.last_event_id,
                    payload_json=excluded.payload_json,
                    update_count=todos.update_count+1,
                    updated_at=excluded.updated_at
                """,
                (
                    event.idempotency_key,
                    event.project,
                    todo.get("external_id"),
                    todo["title"],
                    todo["type"],
                    todo["status"],
                    todo.get("priority"),
                    event.correlation_id,
                    event.event_id,
                    payload,
                    now,
                    now,
                ),
            )
        return event.idempotency_key

    def get(self, idempotency_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM todos WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            return dict(row) if row else None

    def count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0])


def process_batch(
    queue: DurableSQLiteQueue,
    sink: TodoSink,
    *,
    limit: int = 10,
    lease_seconds: int = 60,
    backoff_seconds: float = 1.0,
    now: float | None = None,
) -> dict[str, int]:
    reserved = queue.reserve(limit=limit, lease_seconds=lease_seconds, now=now)
    result = {"reserved": len(reserved), "processed": 0, "retried": 0, "dlq": 0}
    for item in reserved:
        try:
            sink.upsert(item.event)
        except Exception as exc:
            target = queue.fail(
                item.event.event_id,
                str(exc),
                backoff_seconds=backoff_seconds,
                now=now,
            )
            if target == "DLQ":
                result["dlq"] += 1
            else:
                result["retried"] += 1
        else:
            queue.ack(item.event.event_id, now=now)
            result["processed"] += 1
    return result

"""Crash/replay controls for the existing local TODO sink; no external services."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from scripts.todo_event_bus import DurableSQLiteQueue, SQLiteTodoSink, TodoEvent, make_idempotency_key, process_batch


def event(event_id="evt-owned-replay-0001", status="PENDENTE", key=None):
    return TodoEvent.from_dict({
        "schema_version": "1.0", "event_id": event_id, "event_type": "todo.updated",
        "occurred_at": "2026-10-19T00:00:00+00:00", "correlation_id": "corr-owned-replay-20261019",
        "idempotency_key": key or make_idempotency_key("lab", "Teste", "native-todo"),
        "project": "lab", "producer": "isolated-test", "todo": {
            "title": "Synthetic native TODO", "type": "Teste", "status": status,
        },
    })


def receipts(path):
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT count(*) FROM todo_sink_event_receipts").fetchone()[0]


def test_replay_does_not_change_any_todo_field(tmp_path):
    sink = SQLiteTodoSink(tmp_path / "todos.sqlite")
    e = event()
    sink.upsert(e)
    before = sink.get(e.idempotency_key)
    sink.upsert(e)
    assert sink.get(e.idempotency_key) == before
    assert receipts(sink.path) == 1


def test_late_replay_does_not_restore_obsolete_state(tmp_path):
    sink = SQLiteTodoSink(tmp_path / "todos.sqlite")
    first, second = event(), event("evt-owned-replay-0002", "EM ANDAMENTO")
    sink.upsert(first); sink.upsert(second)
    before = sink.get(second.idempotency_key)
    sink.upsert(first)
    assert sink.get(second.idempotency_key) == before
    assert before["update_count"] == 2 and receipts(sink.path) == 2


@pytest.mark.parametrize("changed_key", [False, True])
def test_event_identity_collision_is_rejected_without_changes(tmp_path, changed_key):
    sink = SQLiteTodoSink(tmp_path / "todos.sqlite")
    original = event(); sink.upsert(original)
    modified = event(status="EM ANDAMENTO", key=("a" * 64 if changed_key else None))
    before = sink.get(original.idempotency_key)
    with pytest.raises(ValueError, match="event_id conflict"):
        sink.upsert(modified)
    assert sink.get(original.idempotency_key) == before
    assert sink.count() == 1 and receipts(sink.path) == 1


def test_concurrent_replay_is_one_application(tmp_path):
    sink = SQLiteTodoSink(tmp_path / "todos.sqlite")
    e = event()
    with ThreadPoolExecutor(max_workers=6) as workers:
        results = list(workers.map(sink.upsert, [e] * 12))
    assert set(results) == {e.idempotency_key}
    assert sink.count() == 1
    assert sink.get(e.idempotency_key)["update_count"] == 1
    assert receipts(sink.path) == 1


def test_failed_todo_write_rolls_back_its_receipt(tmp_path):
    sink = SQLiteTodoSink(tmp_path / "todos.sqlite")
    e = event()
    with sqlite3.connect(sink.path) as conn:
        conn.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON todos BEGIN SELECT RAISE(ABORT, 'controlled'); END")
    with pytest.raises(sqlite3.DatabaseError):
        sink.upsert(e)
    assert sink.count() == 0 and receipts(sink.path) == 0
    with sqlite3.connect(sink.path) as conn:
        conn.execute("DROP TRIGGER fail_insert")
    sink.upsert(e)
    assert sink.count() == 1 and receipts(sink.path) == 1


def legacy_sink(tmp_path):
    path = tmp_path / "legacy.sqlite"
    sink = SQLiteTodoSink(path)
    e = event(); sink.upsert(e)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE IF EXISTS todo_sink_event_receipts")
    return path, e, sink.get(e.idempotency_key)


def test_latest_legacy_event_is_backfilled_without_reapplying(tmp_path):
    path, e, before = legacy_sink(tmp_path)
    sink = SQLiteTodoSink(path); sink.upsert(e)
    assert sink.get(e.idempotency_key) == before
    assert receipts(path) == 1


def test_new_event_preserves_latest_legacy_receipt_for_later_replay(tmp_path):
    path, first, _ = legacy_sink(tmp_path)
    second = event("evt-owned-replay-0002", "EM ANDAMENTO")
    sink = SQLiteTodoSink(path); sink.upsert(second)
    before = sink.get(second.idempotency_key)
    sink.upsert(first)
    assert sink.get(second.idempotency_key) == before
    assert receipts(path) == 2


def test_legacy_reused_id_with_different_payload_is_rejected(tmp_path):
    path, e, before = legacy_sink(tmp_path)
    sink = SQLiteTodoSink(path)
    with pytest.raises(ValueError, match="event_id conflict"):
        sink.upsert(event(status="EM ANDAMENTO"))
    assert sink.get(e.idempotency_key) == before and receipts(path) == 0


def test_canonical_json_key_order_does_not_create_a_second_effect(tmp_path):
    sink = SQLiteTodoSink(tmp_path / "todos.sqlite")
    e = event(); sink.upsert(e)
    before = sink.get(e.idempotency_key)
    payload = e.to_dict(); payload["todo"] = dict(reversed(list(payload["todo"].items())))
    sink.upsert(TodoEvent.from_dict(payload))
    assert sink.get(e.idempotency_key) == before


def test_process_crash_after_commit_before_ack_replays_without_effect(tmp_path):
    queue_path, sink_path = tmp_path / "queue.sqlite", tmp_path / "todos.sqlite"
    queue = DurableSQLiteQueue(queue_path)
    e = event(); assert queue.enqueue(e, now=100)
    assert len(queue.reserve(limit=1, lease_seconds=5, now=100)) == 1
    child = """
import json, os, sys
from scripts.todo_event_bus import SQLiteTodoSink, TodoEvent
payload = json.loads(sys.stdin.read())
SQLiteTodoSink(sys.argv[1]).upsert(TodoEvent.from_dict(payload))
os._exit(23)
"""
    safe_env = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "WINDIR") if name in os.environ}
    safe_env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run([sys.executable, "-c", child, str(sink_path)],
                            input=json.dumps(e.to_dict()), text=True, capture_output=True,
                            timeout=20, env=safe_env, check=False)
    assert result.returncode == 23, result.stderr
    with sqlite3.connect(sink_path) as reader:
        before = reader.execute("SELECT payload_json,update_count,updated_at FROM todos").fetchone()
        assert before[1] == 1
    reopened = DurableSQLiteQueue(queue_path)
    assert reopened.recover_expired_leases(now=106) == 1
    assert process_batch(reopened, SQLiteTodoSink(sink_path), now=106)["processed"] == 1
    with sqlite3.connect(sink_path) as reader:
        assert reader.execute("SELECT payload_json,update_count,updated_at FROM todos").fetchone() == before
    assert reopened.state(e.event_id)["state"] == "PROCESSED"
    assert reopened.enqueue(e, now=107) is False
    assert SQLiteTodoSink(sink_path).count() == 1 and receipts(sink_path) == 1

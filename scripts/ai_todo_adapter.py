#!/usr/bin/env python3
"""Adapta solicitações de IAs para o contrato TodoEvent v1."""
from __future__ import annotations

import hashlib
from typing import Any

from scripts.todo_event_bus import TodoEvent, make_idempotency_key, utc_now_iso

SUPPORTED_PRODUCERS = {"chatgpt", "claude", "copilot", "local-agent"}


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


def adapt_ai_request(data: dict[str, Any]) -> TodoEvent:
    producer = str(data.get("producer") or "").strip().lower()
    if producer not in SUPPORTED_PRODUCERS:
        raise ValueError("producer de IA não suportado")
    project = str(data.get("project") or "").strip()
    external_id = str(data.get("external_id") or "").strip()
    title = str(data.get("title") or "").strip()
    if not project or not external_id or not title:
        raise ValueError("project, external_id e title são obrigatórios")
    item_type = str(data.get("type") or "Solicitação IA").strip()
    correlation_id = str(data.get("correlation_id") or "").strip()
    if not correlation_id:
        correlation_id = _stable_id("corr", f"{producer}|{external_id}")
    event_id = str(data.get("event_id") or "").strip()
    if not event_id:
        event_id = _stable_id("evt", f"{producer}|{external_id}")
    payload = {
        "schema_version": "1.0",
        "event_id": event_id,
        "event_type": str(data.get("event_type") or "todo.created"),
        "occurred_at": str(data.get("occurred_at") or utc_now_iso()),
        "correlation_id": correlation_id,
        "idempotency_key": make_idempotency_key(project, item_type, external_id),
        "project": project,
        "producer": producer,
        "todo": {
            "title": title,
            "type": item_type,
            "external_id": external_id,
            "status": str(data.get("status") or "PENDENTE"),
            "priority": str(data.get("priority") or "P1"),
        },
    }
    for field in ("blocker", "next_action", "completion_criteria", "evidence"):
        if data.get(field):
            payload["todo"][field] = str(data[field])
    return TodoEvent.from_dict(payload)

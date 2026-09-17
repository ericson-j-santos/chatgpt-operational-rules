#!/usr/bin/env python3
"""Normaliza fontes externas autorizadas para o adaptador TodoEvent v1."""
from __future__ import annotations

from typing import Any

from scripts.ai_todo_adapter import adapt_ai_request

SOURCE_TO_PRODUCER = {
    "teams": "local-agent",
    "gitlab": "local-agent",
}


def adapt_external_source(source: str, data: dict[str, Any]):
    source = source.strip().lower()
    if source not in SOURCE_TO_PRODUCER:
        raise ValueError("fonte externa não suportada")
    source_id = str(data.get("external_id") or "").strip()
    if not source_id:
        raise ValueError("external_id é obrigatório")
    normalized = dict(data)
    normalized["producer"] = SOURCE_TO_PRODUCER[source]
    normalized["external_id"] = f"{source}:{source_id}"
    normalized.setdefault("type", "Solicitação externa")
    normalized.setdefault("project", "AI Hub")
    return adapt_ai_request(normalized)

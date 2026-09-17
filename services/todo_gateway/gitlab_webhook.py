from __future__ import annotations

import hmac
from typing import Any

from scripts.source_todo_adapter import adapt_external_source

SUPPORTED_EVENTS = {"Push Hook", "Merge Request Hook", "Issue Hook"}


def authorized(expected_token: str, supplied_token: str | None) -> bool:
    if not expected_token or not supplied_token:
        return False
    return hmac.compare_digest(expected_token.encode(), supplied_token.encode())


def normalize(event_name: str, event_uuid: str | None, body: dict[str, Any], expected_project: str | None = None):
    if event_name not in SUPPORTED_EVENTS:
        raise ValueError("evento GitLab não suportado")
    project = body.get("project") or {}
    project_path = str(project.get("path_with_namespace") or "").strip()
    project_id = str(project.get("id") or "").strip()
    if expected_project and project_path.casefold() != expected_project.strip().casefold():
        raise PermissionError("projeto GitLab não autorizado")
    object_kind = str(body.get("object_kind") or "").strip()
    object_attributes = body.get("object_attributes") or {}
    source_id = str(event_uuid or object_attributes.get("id") or body.get("checkout_sha") or body.get("after") or "").strip()
    if not source_id:
        raise ValueError("identificador GitLab ausente")
    title = str(object_attributes.get("title") or body.get("ref") or event_name).strip()
    return adapt_external_source("gitlab", {
        "external_id": source_id,
        "event_id": event_uuid or "",
        "correlation_id": event_uuid or "",
        "project": project_path or (f"gitlab-project-{project_id}" if project_id else "AI Hub"),
        "type": f"GitLab {object_kind or event_name}",
        "title": title,
        "status": "PENDENTE",
        "priority": "P1",
        "next_action": "Processar evento GitLab no TODO Global",
    })

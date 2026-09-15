from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from scripts.todo_event_bus import TodoEvent

NOTION_API_VERSION = "2026-03-11"
TRANSIENT_HTTP = {429, 500, 502, 503, 504}


class TransientSinkError(RuntimeError):
    pass


class PermanentSinkError(RuntimeError):
    pass


class VerificationError(TransientSinkError):
    pass


def _rich_text(value: str | None) -> dict[str, Any]:
    return {"rich_text": [{"type": "text", "text": {"content": value or ""}}]}


def _title(value: str) -> dict[str, Any]:
    return {"title": [{"type": "text", "text": {"content": value}}]}


def _select(value: str | None) -> dict[str, Any] | None:
    return {"select": {"name": value}} if value else None


def _url(value: str | None) -> dict[str, Any] | None:
    return {"url": value} if value else None


def _plain_rich_text(prop: dict[str, Any] | None) -> str:
    if not prop:
        return ""
    chunks = prop.get("rich_text") or []
    return "".join(chunk.get("plain_text") or chunk.get("text", {}).get("content", "") for chunk in chunks)


def _select_name(prop: dict[str, Any] | None) -> str:
    if not prop:
        return ""
    selected = prop.get("select")
    return (selected or {}).get("name", "")


@dataclass
class NotionTodoSink:
    token: str
    data_source_id: str
    client: httpx.Client | None = None

    def __post_init__(self) -> None:
        if not self.token or not self.data_source_id:
            raise ValueError("Notion token and data source id are required")
        if self.client is None:
            self.client = httpx.Client(
                base_url="https://api.notion.com",
                timeout=15.0,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Notion-Version": NOTION_API_VERSION,
                    "Content-Type": "application/json",
                },
            )

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        assert self.client is not None
        response = self.client.request(method, path, **kwargs)
        if response.status_code in TRANSIENT_HTTP:
            raise TransientSinkError(f"Notion transient HTTP {response.status_code}")
        if response.status_code >= 400:
            raise PermanentSinkError(f"Notion HTTP {response.status_code}")
        return response

    def _find_page(self, key: str) -> str | None:
        response = self._request(
            "POST",
            f"/v1/data_sources/{self.data_source_id}/query",
            json={
                "filter": {
                    "property": "Chave de idempotência",
                    "rich_text": {"equals": key},
                },
                "page_size": 2,
            },
        )
        results = response.json().get("results", [])
        if len(results) > 1:
            raise PermanentSinkError("duplicate canonical TODOs for idempotency key")
        return results[0]["id"] if results else None

    def _properties(self, event: TodoEvent) -> dict[str, Any]:
        todo = event.todo
        source = todo.get("source") or "Outra"
        if source not in {"ChatGPT", "ReqSys", "GitHub", "Notion", "Outra"}:
            source = "Outra"
        props: dict[str, Any] = {
            "Título": _title(todo["title"]),
            "Projeto": _rich_text(event.project),
            "Status": _select(todo["status"]),
            "Tipo": _select(todo["type"]),
            "Fonte": _select(source),
            "Identificador externo": _rich_text(todo.get("external_id")),
            "Chave de idempotência": _rich_text(event.idempotency_key),
            "Correlation ID": _rich_text(event.correlation_id),
            "Origem": _rich_text(todo.get("origin")),
            "Bloqueio": _rich_text(todo.get("blocker")),
            "Próxima ação": _rich_text(todo.get("next_action")),
            "Critério de conclusão": _rich_text(todo.get("completion_criteria")),
            "Evidência": _rich_text(todo.get("evidence")),
        }
        optional = {
            "Prioridade": _select(todo.get("priority")),
            "Status E2E": _select(todo.get("e2e_status")),
            "Origem URL": _url(todo.get("origin_url")),
            "Evidência URL": _url(todo.get("evidence_url")),
        }
        props.update({k: v for k, v in optional.items() if v is not None})
        return props

    def upsert(self, event: TodoEvent) -> str:
        page_id = self._find_page(event.idempotency_key)
        properties = self._properties(event)
        if page_id:
            response = self._request(
                "PATCH",
                f"/v1/pages/{page_id}",
                json={"properties": properties},
            )
            page_id = response.json()["id"]
        else:
            response = self._request(
                "POST",
                "/v1/pages",
                json={
                    "parent": {"type": "data_source_id", "data_source_id": self.data_source_id},
                    "properties": properties,
                },
            )
            page_id = response.json()["id"]

        self._verify(page_id, event)
        return page_id

    def _verify(self, page_id: str, event: TodoEvent) -> None:
        response = self._request("GET", f"/v1/pages/{page_id}")
        properties = response.json().get("properties", {})
        key = _plain_rich_text(properties.get("Chave de idempotência"))
        correlation_id = _plain_rich_text(properties.get("Correlation ID"))
        status = _select_name(properties.get("Status"))
        if (
            key != event.idempotency_key
            or correlation_id != event.correlation_id
            or status != event.todo["status"]
        ):
            raise VerificationError("independent Notion readback did not match event")

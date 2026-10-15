from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
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


def _select(value: str | None) -> dict[str, Any]:
    return {"select": {"name": value} if value else None}


def _url(value: str | None) -> dict[str, Any]:
    return {"url": value or None}


def _property_value(prop: Any, kind: str) -> Any:
    """Compare semantic values, never server IDs, colors or formatting."""
    if not isinstance(prop, dict) or kind not in prop:
        raise VerificationError("independent Notion readback missing property shape")
    value = prop[kind]
    if kind in {"rich_text", "title"}:
        if not isinstance(value, list):
            raise VerificationError("independent Notion readback invalid text")
        parts = []
        for chunk in value:
            if not isinstance(chunk, dict):
                raise VerificationError("independent Notion readback invalid text chunk")
            text = chunk.get("plain_text")
            if text is None:
                text_obj = chunk.get("text")
                text = text_obj.get("content") if isinstance(text_obj, dict) else None
            if not isinstance(text, str):
                raise VerificationError("independent Notion readback incomplete text")
            parts.append(text)
        return "".join(parts)
    if kind == "select":
        if value is None:
            return None
        if not isinstance(value, dict) or not isinstance(value.get("name"), str):
            raise VerificationError("independent Notion readback invalid selection")
        return value["name"]
    if kind == "url":
        if value is not None and not isinstance(value, str):
            raise VerificationError("independent Notion readback invalid URL")
        return value
    raise VerificationError("unsupported property in independent Notion readback")


def _properties_match(actual: Any, expected: dict[str, Any]) -> bool:
    if not isinstance(actual, dict):
        raise VerificationError("independent Notion readback missing properties")
    for name, prop in expected.items():
        kind = next(iter(prop))
        if _property_value(actual.get(name), kind) != _property_value(prop, kind):
            return False
    return True


def _version(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise PermanentSinkError("expected page version must be a timestamp with timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PermanentSinkError("expected page version is invalid") from exc
    if parsed.tzinfo is None:
        raise PermanentSinkError("expected page version requires timezone")
    return parsed


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
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise PermanentSinkError("invalid canonical TODO lookup")
        results = data["results"]
        if data.get("has_more", False) is not False or len(results) > 1:
            raise PermanentSinkError("duplicate or incomplete canonical TODO lookup")
        if not results:
            return None
        page_id = results[0].get("id") if isinstance(results[0], dict) else None
        if not isinstance(page_id, str) or not page_id or any(c in page_id for c in "/?#"):
            raise PermanentSinkError("invalid canonical TODO page identity")
        return page_id

    def _properties(self, event: TodoEvent) -> dict[str, Any]:
        todo = event.todo
        props: dict[str, Any] = {
            "Título": _title(todo["title"]),
            "Projeto": _rich_text(event.project),
            "Status": _select(todo["status"]),
            "Tipo": _select(todo["type"]),
            "Chave de idempotência": _rich_text(event.idempotency_key),
            "Correlation ID": _rich_text(event.correlation_id),
        }
        # An omitted field is NOT an instruction to erase existing evidence.
        # Explicit None/empty values remain an intentional clear operation.
        text_fields = {
            "external_id": "Identificador externo", "origin": "Origem",
            "blocker": "Bloqueio", "next_action": "Próxima ação",
            "completion_criteria": "Critério de conclusão", "evidence": "Evidência",
        }
        for field, name in text_fields.items():
            if field in todo:
                props[name] = _rich_text(todo[field])
        for field, name in {"priority": "Prioridade", "e2e_status": "Status E2E"}.items():
            if field in todo:
                props[name] = _select(todo[field])
        for field, name in {"origin_url": "Origem URL", "evidence_url": "Evidência URL"}.items():
            if field in todo:
                props[name] = _url(todo[field])
        if "source" in todo:
            source = todo["source"] or "Outra"
            if source not in {"ChatGPT", "ReqSys", "GitHub", "Notion", "Outra"}:
                source = "Outra"
            props["Fonte"] = _select(source)
        return props

    def _read_page(self, page_id: str) -> dict[str, Any]:
        page = self._request("GET", f"/v1/pages/{page_id}").json()
        if (
            not isinstance(page, dict) or page.get("id") != page_id
            or any(page.get(flag) for flag in ("archived", "is_archived", "in_trash"))
            or not isinstance(page.get("properties"), dict)
        ):
            raise VerificationError("independent Notion readback invalid page")
        return page

    def upsert(self, event: TodoEvent) -> str:
        # This detects stale snapshots; it is NOT a server-side compare-and-swap.
        # All writers still require the existing shared idempotency lock.
        guarded = "expected_page_last_edited_at" in event.todo
        expected_version = _version(event.todo["expected_page_last_edited_at"]) if guarded else None
        page_id = self._find_page(event.idempotency_key)
        properties = self._properties(event)
        if page_id:
            current = self._read_page(page_id)
            try:
                unchanged = _properties_match(current["properties"], properties)
            except VerificationError:
                unchanged = False
            if unchanged:
                return page_id  # Read-only replay: no PATCH, no timestamp churn.
            if guarded and _version(current.get("last_edited_time")) != expected_version:
                raise PermanentSinkError("canonical TODO changed; refresh snapshot before retry")
            response = self._request("PATCH", f"/v1/pages/{page_id}", json={"properties": properties})
            if response.json().get("id") != page_id:
                raise VerificationError("Notion write returned a different page identity")
        else:
            if guarded:
                raise PermanentSinkError("guarded canonical TODO no longer exists")
            response = self._request(
                "POST", "/v1/pages",
                json={
                    "parent": {"type": "data_source_id", "data_source_id": self.data_source_id},
                    "properties": properties,
                },
            )
            page_id = response.json().get("id")
            if not isinstance(page_id, str) or not page_id or any(c in page_id for c in "/?#"):
                raise VerificationError("Notion create returned an invalid page identity")
        self._verify(page_id, event)
        return page_id

    def _verify(self, page_id: str, event: TodoEvent) -> None:
        page = self._read_page(page_id)
        if not _properties_match(page["properties"], self._properties(event)):
            # Do not put field values (potentially confidential) into logs/DLQ.
            raise VerificationError("independent Notion readback did not match all supplied fields")

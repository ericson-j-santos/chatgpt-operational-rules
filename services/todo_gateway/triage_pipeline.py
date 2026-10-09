"""DEV-only bridge: verified sources -> reservation -> existing queue -> Notion.

The PostgreSQL checkpoint and queue insertion share one transaction. Notion is
an independently verified projection, never part of that database transaction.
"""
from __future__ import annotations

import copy
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import UUID, uuid4

import httpx
from fastapi import Request

from scripts.todo_event_bus import TodoEvent
from services.todo_gateway.notion_sink import (
    NotionTodoSink, PermanentSinkError, TransientSinkError, VerificationError,
    _properties_match, _version,
)
from services.todo_gateway.repository import PostgresQueueRepository
from services.todo_gateway.triage_reservation import (
    OPERATION, ReservationConflict, TriageReservations, canonical, digest,
    identity, integer, names, scope_for, source_version, validate_record,
)

PRODUCER = "triage-publisher-v1"
META = "triage_projection"
HASH = re.compile(r"[0-9a-f]{64}\Z")


def page_uuid(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid projection page identity")
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise ValueError("invalid projection page identity") from exc


def queue_key(key: str) -> str:
    scope_for(key)
    return key if HASH.fullmatch(key) else scope_for(key)


def event_id_for(key: str, claim: str) -> str:
    identity(claim)
    return "triage-" + digest([OPERATION, key, claim])


def text_items(value: Any) -> list[dict]:
    """Preserve text, links and annotations; unsupported rich text fails closed."""
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("unsupported evidence rich-text shape")
    output = []
    for item in value:
        if not isinstance(item, dict) or item.get("type", "text") != "text":
            raise ValueError("evidence contains an unsupported rich-text type")
        text = item.get("text")
        if (not isinstance(text, dict) or not isinstance(text.get("content"), str)
                or len(text["content"]) > 2000):
            raise ValueError("invalid evidence text")
        clean = {"type": "text", "text": {"content": text["content"]}}
        if text.get("link") is not None:
            link = text["link"]
            if not isinstance(link, dict) or not isinstance(link.get("url"), str):
                raise ValueError("invalid evidence link")
            clean["text"]["link"] = {"url": link["url"]}
        if "annotations" in item:
            annotations = item["annotations"]
            allowed = {"bold", "italic", "strikethrough", "underline", "code", "color"}
            if not isinstance(annotations, dict) or set(annotations) - allowed:
                raise ValueError("unsupported evidence annotations")
            for field, val in annotations.items():
                if field == "color":
                    if not isinstance(val, str):
                        raise ValueError("invalid evidence color")
                elif type(val) is not bool:
                    raise ValueError("invalid evidence annotation")
            clean["annotations"] = copy.deepcopy(annotations)
        output.append(clean)
    return output


def plain(items: list[dict]) -> str:
    return "".join(item["text"]["content"] for item in items)


def prop_text(page: dict, name: str, kind: str = "rich_text") -> str:
    prop = page.get("properties", {}).get(name)
    if not isinstance(prop, dict) or kind not in prop:
        raise ValueError("required source property is absent")
    return plain(text_items(prop[kind]))


def prop_select(page: dict, name: str) -> str:
    value = page.get("properties", {}).get(name, {}).get("select")
    if not isinstance(value, dict) or not isinstance(value.get("name"), str):
        raise ValueError("required source selection is absent")
    return value["name"]


def validate_target(page: dict, page_id: str, data_source_id: str, key: str) -> None:
    parent = page.get("parent", {})
    if (page_uuid(page.get("id")) != page_uuid(page_id)
            or parent.get("type") != "data_source_id"
            or page_uuid(parent.get("data_source_id")) != page_uuid(data_source_id)
            or any(page.get(flag) for flag in ("archived", "in_trash", "is_archived"))
            or prop_text(page, "Chave de idempotência") != key):
        raise PermanentSinkError("canonical projection target changed")


def projection_meta(event: TodoEvent) -> dict:
    meta = event.todo.get(META)
    expected = {"page_id", "canonical_key", "request_sha256", "request_id", "before", "suffix"}
    if not isinstance(meta, dict) or set(meta) != expected or event.producer != PRODUCER:
        raise PermanentSinkError("invalid typed triage projection")
    if event.event_type != "todo.evidence.updated" or event.todo.get("status") not in {"PENDENTE", "EM ANDAMENTO"}:
        raise PermanentSinkError("invalid triage projection event type or state")
    page_uuid(meta["page_id"])
    scope_for(meta["canonical_key"])
    identity(meta["request_id"])
    if (not isinstance(meta["request_sha256"], str) or not HASH.fullmatch(meta["request_sha256"])
            or event.idempotency_key != queue_key(meta["canonical_key"])
            or event.event_id != event_id_for(meta["canonical_key"], meta["request_id"])):
        raise PermanentSinkError("projection identity mismatch")
    suffix = meta["suffix"]
    if not isinstance(suffix, str) or not 1 <= len(suffix) <= 2000:
        raise PermanentSinkError("invalid projection evidence suffix")
    before = text_items(meta["before"])
    if len(before) > 99:
        raise PermanentSinkError("evidence has no room for an appended chunk")
    if event.todo.get("evidence") != plain(before) + suffix or len(event.todo["evidence"]) > 4000:
        raise PermanentSinkError("projection evidence mismatch")
    _version(event.todo.get("expected_page_last_edited_at"))
    return meta


class TriageProjectionSink(NotionTodoSink):
    """Legacy events retain the existing writer; triage can only PATCH one page."""

    def __init__(self, token: str, data_source_id: str, client=None, *, queue=None):
        super().__init__(token, data_source_id, client)
        self.queue = queue

    def require_committed_event(self, event: TodoEvent) -> None:
        """A forged queue envelope is not a reservation or authorization to write."""
        meta = projection_meta(event)
        if self.queue is None:
            raise PermanentSinkError("triage writer requires the reservation ledger")
        with self.queue._connect() as conn:
            row = conn.execute(
                "SELECT checkpoint FROM todo_bus.triage_reservations WHERE scope_key=%s",
                (scope_for(meta["canonical_key"]),),
            ).fetchone()
            queued = conn.execute(
                "SELECT payload FROM todo_bus.queue_events WHERE event_id=%s",
                (event.event_id,),
            ).fetchone()
        if (not row or not queued or row[0]["active"] is not None
                or row[0].get("projection", {}).get("event_id") != event.event_id
                or event.event_id not in row[0].get("receipts", {})
                or canonical(queued[0]) != canonical(event.to_dict())):
            raise PermanentSinkError("projection does not match the committed reservation")

    def _request(self, method: str, path: str, **kwargs: Any):
        # Surface a server-requested delay to the existing queue worker, not a new retry loop.
        assert self.client is not None
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise TransientSinkError("Notion transport unavailable") from exc
        if response.status_code in {429, 529}:
            error = TransientSinkError("Notion projection rate limited")
            value = response.headers.get("Retry-After", "60")
            if not value.isdecimal() or int(value) > 86400:
                raise PermanentSinkError("Notion retry delay requires operator review")
            error.retry_after_seconds = max(1, int(value))
            raise error
        if response.status_code in {500, 502, 503, 504}:
            raise TransientSinkError("Notion projection temporarily unavailable")
        if response.status_code >= 400:
            raise PermanentSinkError(f"Notion HTTP {response.status_code}")
        return response

    def expected_properties(self, event: TodoEvent) -> dict:
        meta = projection_meta(event)
        return {"Evidência": {"rich_text": text_items(meta["before"]) + [
            {"type": "text", "text": {"content": meta["suffix"]}},
        ]}}

    def verify_projection(self, event: TodoEvent) -> dict:
        meta = projection_meta(event)
        self.require_committed_event(event)
        found = self._find_page(meta["canonical_key"])
        if found is None or page_uuid(found) != page_uuid(meta["page_id"]):
            raise PermanentSinkError("canonical TODO missing or duplicated; never create a replacement")
        page = self._read_page(found)
        validate_target(page, meta["page_id"], self.data_source_id, meta["canonical_key"])
        expected = self.expected_properties(event)
        if not _properties_match(page["properties"], expected):
            raise VerificationError("independent triage evidence readback differs")
        # Verify preserved formatting/links as well as plain text. Notion may add default annotations.
        actual = text_items(page["properties"]["Evidência"]["rich_text"])
        def semantic(items):
            merged = []
            for item in items:
                annotations = {k: v for k, v in item.get("annotations", {}).items()
                               if v not in (False, "default")}
                style = [item["text"].get("link"), annotations]
                if merged and merged[-1][1] == style:
                    merged[-1][0] += item["text"]["content"]
                else:
                    merged.append([item["text"]["content"], style])
            return merged
        if semantic(actual) != semantic(expected["Evidência"]["rich_text"]):
            raise VerificationError("independent triage rich-text preservation differs")
        return page

    def upsert(self, event: TodoEvent) -> str:
        if META not in event.todo and event.producer != PRODUCER:
            return super().upsert(event)
        meta = projection_meta(event)
        self.require_committed_event(event)
        found = self._find_page(meta["canonical_key"])
        if found is None or page_uuid(found) != page_uuid(meta["page_id"]):
            raise PermanentSinkError("canonical TODO missing or duplicated; never create a replacement")
        current = self._read_page(found)
        validate_target(current, meta["page_id"], self.data_source_id, meta["canonical_key"])
        expected = self.expected_properties(event)
        if _properties_match(current["properties"], expected):
            self.verify_projection(event)
            return found  # Includes recovery after a write succeeded but its response was lost.
        if _version(current.get("last_edited_time")) != _version(event.todo["expected_page_last_edited_at"]):
            raise PermanentSinkError("TODO changed after enqueue; reconcile without overwriting")
        if text_items(current["properties"]["Evidência"]["rich_text"]) != text_items(meta["before"]):
            raise PermanentSinkError("original evidence changed; no overwrite permitted")
        response = self._request("PATCH", f"/v1/pages/{found}", json={"properties": expected})
        if page_uuid(response.json().get("id")) != page_uuid(found):
            raise VerificationError("Notion returned a different projection identity")
        self.verify_projection(event)
        return found


class _TransactionQueue(PostgresQueueRepository):
    """Borrow one connection. Only the outer owner may commit or roll it back."""

    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def _connect(self):
        yield self.connection


class AtomicProjectionStore:
    def __init__(self, queue: PostgresQueueRepository):
        self.queue = queue

    def lookup(self, event_id: str) -> tuple[TodoEvent, str] | None:
        with self.queue._connect() as conn:
            row = conn.execute("SELECT payload, state FROM todo_bus.queue_events WHERE event_id=%s",
                               (event_id,)).fetchone()
        return (TodoEvent.from_dict(row[0]), row[1]) if row else None

    def claim(self, body: dict) -> dict:
        with self.queue._connect() as conn:
            store = TriageReservations(_TransactionQueue(conn))
            receipt = store.claim(body)
            with store._locked(body["todo_key"]) as (_, _, state, _):
                pending = state.get("projection")
                if pending and pending["state"] != "CONFIRMED":
                    raise ReservationConflict("previous projection awaits independent readback")
            return receipt

    def commit(self, body: dict, event: TodoEvent) -> dict:
        meta = projection_meta(event)
        if meta["canonical_key"] != body["todo_key"] or event.event_id != body["commit_id"]:
            raise ReservationConflict("event does not belong to the reservation")
        with self.queue._connect() as conn:
            tx = _TransactionQueue(conn)
            store = TriageReservations(tx)
            receipt = store.complete(body)
            inserted = tx.enqueue(event)
            persisted = conn.execute(
                "SELECT payload FROM todo_bus.queue_events WHERE event_id=%s FOR UPDATE",
                (event.event_id,),
            ).fetchone()
            if not persisted:
                raise ReservationConflict("queue event was not persisted")
            expected_payload, actual_payload = event.to_dict(), dict(persisted[0])
            # Concurrent delivery of the same request can have a different emission time.
            # Keep the FIRST durable event; every other field must remain identical.
            actual_payload.pop("occurred_at", None)
            expected_payload.pop("occurred_at", None)
            if canonical(actual_payload) != canonical(expected_payload):
                raise ReservationConflict("queue event identity conflicts with stored payload")
            with store._locked(body["todo_key"]) as (cur, scope, state, _):
                if receipt["replayed"]:
                    if state.get("projection", {}).get("event_id") != event.event_id:
                        raise ReservationConflict("checkpoint replay has another projection")
                else:
                    if not inserted:
                        raise ReservationConflict("unexpected pre-existing projection event")
                    state["projection"] = {"event_id": event.event_id, "state": "QUEUED"}
                    store._save(cur, scope, state, receipt["version"])
        return {**receipt, "event_id": event.event_id, "external_projection": "QUEUED"}

    def confirm(self, event: TodoEvent, page: dict) -> None:
        meta = projection_meta(event)
        with self.queue._connect() as conn:
            store = TriageReservations(_TransactionQueue(conn))
            with store._locked(meta["canonical_key"]) as (cur, scope, state, _):
                status = conn.execute("SELECT state FROM todo_bus.queue_events WHERE event_id=%s",
                                      (event.event_id,)).fetchone()
                if (not status or status[0] != "PROCESSED" or state["active"] is not None
                        or state.get("projection", {}).get("event_id") != event.event_id):
                    raise ReservationConflict("projection receipt is not the current acknowledged event")
                state["projection"]["state"] = "CONFIRMED"
                state["source_version"] = source_version(page["last_edited_time"])
                store._save(cur, scope, state, state["version"])


class TriagePublisher:
    def __init__(self, queue, notion: TriageProjectionSink, github: httpx.Client,
                 page_id: str, owner: str):
        self.queue = queue
        self.store = AtomicProjectionStore(queue)
        self.notion = notion
        self.github = github
        self.page_id = page_uuid(page_id)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", owner):
            raise ValueError("invalid allowed repository owner")
        self.owner = owner.lower()
        if str(github.base_url).rstrip("/") != "https://api.github.com":
            raise ValueError("GitHub source must use the official API origin")

    def source(self, key: str) -> dict:
        found = self.notion._find_page(key)
        if found is None or page_uuid(found) != self.page_id:
            raise ReservationConflict("configured canonical TODO was not found uniquely")
        page = self.notion._read_page(found)
        validate_target(page, self.page_id, self.notion.data_source_id, key)
        if prop_select(page, "Status") not in {"PENDENTE", "EM ANDAMENTO"}:
            raise ReservationConflict("canonical TODO is not eligible for new triage")
        return page

    def records(self, repositories: list[str]) -> list[dict]:
        records = []
        for repo in repositories:
            if repo.split("/")[0] != self.owner:
                raise ValueError("repository is outside the configured owner")
            try:
                metadata = self.github.get(f"/repos/{repo}")
                metadata.raise_for_status()
                data = metadata.json()
                if data.get("full_name", "").lower() != repo or data.get("disabled"):
                    raise ValueError("repository source identity changed")
                branch = data["default_branch"]
                value = self.github.get(f"/repos/{repo}/branches/{quote(branch, safe='')}")
                value.raise_for_status()
                reference = value.json()
                if reference.get("name") != branch:
                    raise ValueError("source branch identity changed")
                records.append(validate_record({"repository": repo, "branch": branch,
                                                "head_sha": reference["commit"]["sha"]}))
            except (httpx.HTTPError, KeyError, TypeError) as exc:
                raise TransientSinkError("GitHub source unavailable or incomplete") from exc
        return records

    def resume(self, event: TodoEvent, state: str, fingerprint: str) -> dict:
        meta = projection_meta(event)
        if meta["request_sha256"] != fingerprint or page_uuid(meta["page_id"]) != self.page_id:
            raise ReservationConflict("request identity was reused with different inputs")
        result = {"event_id": event.event_id, "queue_state": state, "replayed": True,
                  "todo_completed": False, "readback_confirmed": False}
        if state == "PROCESSED":
            page = self.notion.verify_projection(event)
            self.store.confirm(event, page)
            result.update(external_projection="CONFIRMED", readback_confirmed=True)
        else:
            result["external_projection"] = "BLOCKED" if state == "DLQ" else "QUEUED"
        return result

    def run(self, body: dict) -> dict:
        required = {"todo_key", "request_id", "correlation_id", "expected_version",
                    "source_version", "inventory", "known"}
        if not isinstance(body, dict) or not required <= set(body) or set(body) - required - {"limit", "lease_seconds"}:
            raise ValueError("invalid typed producer request")
        scope_for(body["todo_key"])
        identity(body["request_id"])
        identity(body["correlation_id"])
        integer(body["expected_version"], 0, 1000000)
        integer(body.get("limit", 10), 1, 10)
        integer(body.get("lease_seconds", 300), 1, 300)
        source_version(body["source_version"])
        inventory, known = names(body["inventory"]), names(body["known"])
        if not set(known) <= set(inventory) or any(r.split("/")[0] != self.owner for r in inventory):
            raise ValueError("inventory is outside the configured scope")
        fingerprint = digest(body)
        event_id = event_id_for(body["todo_key"], body["request_id"])
        saved = self.store.lookup(event_id)
        if saved:
            return self.resume(*saved, fingerprint)
        source = self.source(body["todo_key"])
        if (_version(source["last_edited_time"]) != _version(body["source_version"])
                or prop_text(source, "Correlation ID") != body["correlation_id"]):
            raise ReservationConflict("producer snapshot or correlation is stale")
        # A durable request and an expiring lease have DIFFERENT identities.
        # A released/expired attempt can be retried without a new logical event.
        claim_body = {key: value for key, value in body.items() if key != "request_id"}
        claim_body["claim_id"] = "claim-" + uuid4().hex
        receipt = self.store.claim(claim_body)
        if receipt["state"] == "NO_WORK":
            return receipt
        committed = False
        try:
            records = self.records(receipt["candidates"])
            second = self.source(body["todo_key"])
            if (source != second or records != self.records(receipt["candidates"])):
                raise ReservationConflict("GitHub or Notion source changed during verification")
            before = text_items(source["properties"]["Evidência"]["rich_text"])
            suffix = ("\n\nCheckpoint técnico de referências " + event_id
                      + " (não conclui classificação ou E2E de produto):\n"
                      + "\n".join(f'{r["repository"]}@{r["head_sha"]} [{r["branch"]}]' for r in records))
            data = {
                "schema_version": "1.0", "event_id": event_id,
                "event_type": "todo.evidence.updated",
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "correlation_id": body["correlation_id"], "idempotency_key": queue_key(body["todo_key"]),
                "project": prop_text(source, "Projeto"), "producer": PRODUCER,
                "todo": {
                    "title": prop_text(source, "Título", "title"), "type": prop_select(source, "Tipo"),
                    "status": prop_select(source, "Status"), "evidence": plain(before) + suffix,
                    "expected_page_last_edited_at": source["last_edited_time"],
                    META: {"page_id": self.page_id, "canonical_key": body["todo_key"],
                           "request_sha256": fingerprint, "request_id": body["request_id"],
                           "before": before, "suffix": suffix},
                },
            }
            event = TodoEvent.from_dict(data)
            projection_meta(event)  # Limits/identity must pass before the transaction.
            completion = {"todo_key": body["todo_key"], "claim_id": claim_body["claim_id"],
                          "fence": receipt["fence"], "expected_version": receipt["version"],
                          "source_version": body["source_version"], "commit_id": event_id, "records": records}
            result = self.store.commit(completion, event)
            committed = True
            return {**result, "queue_state": "PENDING", "readback_confirmed": False}
        finally:
            if not committed:
                # If commit outcome is uncertain, never issue a second enqueue. Replay looks it up.
                # Release may fail after expiration/commit; fencing ensures it cannot release a successor.
                try:
                    TriageReservations(self.queue).release(
                        {"todo_key": body["todo_key"], "claim_id": claim_body["claim_id"],
                         "fence": receipt["fence"], "expected_version": receipt["version"]})
                except ReservationConflict:
                    pass


def publisher_from_env(queue) -> TriagePublisher:
    if os.environ.get("TODO_TRIAGE_ENVIRONMENT") != "DEV":
        raise RuntimeError("triage publication requires DEV")
    required = ("NOTION_TOKEN", "NOTION_DATA_SOURCE_ID", "TODO_TRIAGE_PAGE_ID", "TODO_TRIAGE_OWNER",
                "PORTFOLIO_GITHUB_TOKEN_FILE")
    if not all(os.environ.get(name) for name in required):
        raise RuntimeError("triage source configuration is incomplete; no source request sent")
    path = Path(os.environ["PORTFOLIO_GITHUB_TOKEN_FILE"])
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("protected GitHub token reference is unavailable")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise RuntimeError("GitHub token file must not be group/world accessible")
    token = path.read_text(encoding="utf-8").strip()
    if not token or "\n" in token or len(token) > 8192:
        raise RuntimeError("invalid protected GitHub token reference")
    github = httpx.Client(base_url="https://api.github.com", timeout=5.0, follow_redirects=False,
                         headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"})
    notion = TriageProjectionSink(os.environ["NOTION_TOKEN"], os.environ["NOTION_DATA_SOURCE_ID"], queue=queue)
    return TriagePublisher(queue, notion, github, os.environ["TODO_TRIAGE_PAGE_ID"],
                           os.environ["TODO_TRIAGE_OWNER"])


def install_pipeline(app: Any, queue: Any, gateway_token: str, publisher=None) -> None:
    from fastapi import Header, HTTPException
    from starlette.concurrency import run_in_threadpool
    from services.todo_gateway.gateway import _require_authorized, json_loads, MAX_BODY_BYTES
    publisher = publisher or publisher_from_env(queue)

    @app.post("/v1/triage/publish")
    async def publish(request: Request, authorization: str | None = Header(default=None)):
        _require_authorized(gateway_token, authorization)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_BODY_BYTES:
                raise HTTPException(413, "payload too large")
        try:
            return await run_in_threadpool(publisher.run, json_loads(bytes(raw)))
        except (ReservationConflict, PermanentSinkError) as exc:
            raise HTTPException(409, "source, target or projection requires reconciliation") from exc
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            raise HTTPException(422, "invalid typed triage request") from exc
        except Exception as exc:
            raise HTTPException(503, "triage publication unavailable; replay the same identity") from exc

    # In publication mode, legacy write routes are deliberately NOT installed.
    @app.get("/v1/triage/snapshot")
    def snapshot(todo_key: str, authorization: str | None = Header(default=None)):
        _require_authorized(gateway_token, authorization)
        try:
            return TriageReservations(queue).snapshot(todo_key)
        except ValueError as exc:
            raise HTTPException(422, "invalid triage identity") from exc
        except Exception as exc:
            raise HTTPException(503, "triage store unavailable") from exc

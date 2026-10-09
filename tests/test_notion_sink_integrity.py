"""Contract tests. MockTransport simulates Notion; this is not live Notion E2E."""
from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace
from pathlib import Path

import httpx

from services.todo_gateway.notion_sink import (
    NotionTodoSink, PermanentSinkError, TransientSinkError, VerificationError,
)

VERSION = "2026-10-09T19:00:00+00:00"


def event(**overrides):
    todo = {
        "title": "Preservar evidência", "type": "Correção", "status": "EM ANDAMENTO",
        "external_id": "issue-137", "origin": "GitHub", "source": "GitHub",
        "blocker": "Acesso externo pendente", "next_action": "Validar integração",
        "completion_criteria": "Readback completo", "evidence": "sha-current",
        "priority": "P1", "e2e_status": "PARCIAL",
        "origin_url": "https://github.com/example/project/issues/137",
        "evidence_url": "https://github.com/example/project/actions/runs/1",
    }
    todo.update(overrides)
    return SimpleNamespace(todo=todo, project="Engineering", idempotency_key="a" * 64,
                           correlation_id="corr-integrity-137", event_id="event-integrity-137")


def text(value, kind="rich_text"):
    return {kind: [{"plain_text": value, "type": "text", "text": {"content": value}}]}


def properties(e):
    """Independent oracle: explicit schema mapping, never calls sink._properties."""
    result = {
        "Título": text(e.todo["title"], "title"), "Projeto": text(e.project),
        "Status": {"select": {"name": e.todo["status"]}},
        "Tipo": {"select": {"name": e.todo["type"]}},
        "Chave de idempotência": text(e.idempotency_key), "Correlation ID": text(e.correlation_id),
    }
    for field, name in (
        ("external_id", "Identificador externo"), ("origin", "Origem"),
        ("blocker", "Bloqueio"), ("next_action", "Próxima ação"),
        ("completion_criteria", "Critério de conclusão"), ("evidence", "Evidência"),
    ):
        if field in e.todo:
            result[name] = text(e.todo[field] or "")
    for field, name in (("priority", "Prioridade"), ("e2e_status", "Status E2E"), ("source", "Fonte")):
        if field in e.todo:
            value = e.todo[field]
            result[name] = {"select": {"name": value} if value else None}
    for field, name in (("origin_url", "Origem URL"), ("evidence_url", "Evidência URL")):
        if field in e.todo:
            result[name] = {"url": e.todo[field] or None}
    return result


class MemoryNotion:
    """Explicitly simulated remote service with an independent persisted dictionary."""
    def __init__(self, initial=None):
        self.page = copy.deepcopy(initial)
        self.writes = []
        self.calls = []
        self.after_write = None
        self.lookup_override = None
        self.returned_id = "page-137"

    def __call__(self, request):
        self.calls.append((request.method, request.url.path))
        if request.url.path.endswith("/query"):
            value = self.lookup_override
            if value is None:
                value = {"results": [{"id": "page-137"}] if self.page is not None else [], "has_more": False}
            return httpx.Response(200, json=value)
        if request.method in {"POST", "PATCH"}:
            body = json.loads(request.content)
            self.writes.append(copy.deepcopy(body))
            if self.page is None:
                self.page = {"id": "page-137", "properties": {}, "last_edited_time": VERSION}
            self.page["properties"].update(copy.deepcopy(body["properties"]))
            if self.after_write:
                self.after_write(self.page)
            return httpx.Response(200, json={"id": self.returned_id})
        return httpx.Response(200, json=copy.deepcopy(self.page))


class IntegrityTests(unittest.TestCase):
    def make(self, e=None, *, exists=False):
        e = e or event()
        initial = {"id": "page-137", "properties": properties(e), "last_edited_time": VERSION} if exists else None
        remote = MemoryNotion(initial)
        client = httpx.Client(base_url="https://api.notion.com", transport=httpx.MockTransport(remote))
        self.addCleanup(client.close)
        return NotionTodoSink("synthetic-test-token", "ds-test", client=client), remote, e

    def test_event_schema_admits_explicit_version_guard(self):
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "schemas/todo-event-v1.schema.json").read_text()
        )
        todo = schema["properties"]["todo"]
        self.assertFalse(todo["additionalProperties"])
        guard = todo["properties"]["expected_page_last_edited_at"]
        self.assertEqual(guard["type"], "string")
        self.assertEqual(guard["format"], "date-time")
        self.assertEqual(guard["maxLength"], 64)
        self.assertIn("title", todo["required"])
        self.assertIn("status", todo["required"])

    def test_create_full_readback(self):
        sink, remote, e = self.make()
        self.assertEqual(sink.upsert(e), "page-137")
        self.assertEqual(len(remote.writes), 1)
        self.assertEqual(remote.calls[-1], ("GET", "/v1/pages/page-137"))

    def test_replay_is_read_only(self):
        sink, remote, e = self.make()
        sink.upsert(e)
        original = copy.deepcopy(remote.page)
        for _ in range(3):
            self.assertEqual(sink.upsert(e), "page-137")
        self.assertEqual(len(remote.writes), 1)
        self.assertEqual(remote.page, original)

    def test_existing_update_changes_only_supplied_fields(self):
        sink, remote, e = self.make(exists=True)
        for field in ("evidence", "blocker", "next_action", "origin", "external_id",
                      "completion_criteria", "priority", "e2e_status", "source", "origin_url", "evidence_url"):
            e.todo.pop(field)
        e.todo["title"] = "Título atualizado"
        before = copy.deepcopy(remote.page["properties"])
        sink.upsert(e)
        sent = remote.writes[-1]["properties"]
        for name in before:
            if name not in {"Título", "Projeto", "Status", "Tipo", "Chave de idempotência", "Correlation ID"}:
                self.assertNotIn(name, sent)
                self.assertEqual(remote.page["properties"][name], before[name])

    def test_explicit_clear(self):
        sink, remote, e = self.make(exists=True)
        e.todo.update(evidence=None, blocker="", priority=None, e2e_status=None, evidence_url=None)
        sink.upsert(e)
        props = remote.page["properties"]
        self.assertEqual(props["Evidência"]["rich_text"][0]["text"]["content"], "")
        self.assertEqual(props["Bloqueio"]["rich_text"][0]["text"]["content"], "")
        self.assertIsNone(props["Prioridade"]["select"])
        self.assertIsNone(props["Status E2E"]["select"])
        self.assertIsNone(props["Evidência URL"]["url"])

    def test_stale_snapshot_blocks_without_write(self):
        sink, remote, e = self.make(exists=True)
        e.todo["title"] = "Novo"
        e.todo["expected_page_last_edited_at"] = "2026-10-09T18:59:59Z"
        with self.assertRaises(PermanentSinkError):
            sink.upsert(e)
        self.assertEqual(remote.writes, [])

    def test_matching_snapshot_allows_update(self):
        sink, remote, e = self.make(exists=True)
        e.todo["title"] = "Novo"
        e.todo["expected_page_last_edited_at"] = "2026-10-09T19:00:00Z"
        sink.upsert(e)
        self.assertEqual(len(remote.writes), 1)

    def test_read_only_replay_after_version_change(self):
        sink, remote, e = self.make(exists=True)
        e.todo["expected_page_last_edited_at"] = "2026-10-09T18:00:00Z"
        self.assertEqual(sink.upsert(e), "page-137")
        self.assertEqual(remote.writes, [])

    def test_guarded_missing_page_does_not_recreate(self):
        sink, remote, e = self.make()
        e.todo["expected_page_last_edited_at"] = VERSION
        with self.assertRaises(PermanentSinkError):
            sink.upsert(e)
        self.assertEqual(remote.writes, [])

    def test_different_write_identity_is_rejected(self):
        sink, remote, e = self.make()
        remote.returned_id = "other-page"
        remote.after_write = lambda p: p.update(id="wrong-page")
        with self.assertRaises(VerificationError):
            sink.upsert(e)

    def test_different_existing_write_identity_is_rejected(self):
        sink, remote, e = self.make(exists=True)
        e.todo["title"] = "Novo"
        remote.returned_id = "other-page"
        with self.assertRaises(VerificationError):
            sink.upsert(e)

    def test_invalid_readback_shape_blocks(self):
        sink, remote, e = self.make(exists=True)
        remote.page.pop("properties")
        with self.assertRaises(VerificationError):
            sink.upsert(e)
        self.assertEqual(remote.writes, [])

    def test_malformed_optional_text_is_rejected(self):
        sink, remote, e = self.make()
        remote.after_write = lambda p: p["properties"].update({"Evidência": {"rich_text": [{"type": "mention"}]}})
        with self.assertRaises(VerificationError):
            sink.upsert(e)

    def test_split_text_and_server_metadata_are_accepted(self):
        sink, remote, e = self.make()
        def split(page):
            page["properties"]["Evidência"] = {"id": "server-id", "type": "rich_text",
                "rich_text": [{"plain_text": "sha-"}, {"plain_text": "current"}]}
            page["properties"]["Prioridade"]["select"].update(id="selection-id", color="red")
        remote.after_write = split
        self.assertEqual(sink.upsert(e), "page-137")

    def test_transient_http(self):
        client = httpx.Client(base_url="https://api.notion.com",
                             transport=httpx.MockTransport(lambda r: httpx.Response(503)))
        self.addCleanup(client.close)
        with self.assertRaises(TransientSinkError):
            NotionTodoSink("synthetic", "ds", client).upsert(event())

    def test_extra_external_properties_are_preserved(self):
        sink, remote, e = self.make(exists=True)
        remote.page["properties"]["Responsável"] = {"people": [{"id": "person-test"}]}
        e.todo["title"] = "Novo"
        sink.upsert(e)
        self.assertEqual(remote.page["properties"]["Responsável"], {"people": [{"id": "person-test"}]})


def corruption_test(name):
    def run(self):
        sink, remote, e = self.make()
        def corrupt(page):
            prop = page["properties"][name]
            if "rich_text" in prop or "title" in prop:
                kind = "rich_text" if "rich_text" in prop else "title"
                prop[kind] = [{"plain_text": "outdated-evidence"}]
            elif "select" in prop:
                prop["select"] = {"name": "outdated-selection"}
            else:
                prop["url"] = "https://example.invalid/outdated"
        remote.after_write = corrupt
        with self.assertRaises(VerificationError):
            sink.upsert(e)
    return run


for number, name in enumerate(properties(event())):
    setattr(IntegrityTests, f"test_corrupted_field_{number:02d}", corruption_test(name))


def invalid_version_test(value):
    def run(self):
        sink, remote, e = self.make()
        e.todo["expected_page_last_edited_at"] = value
        with self.assertRaises(PermanentSinkError):
            sink.upsert(e)
        self.assertEqual(remote.calls, [])
    return run


for number, value in enumerate((None, "", 1, [], "invalid", "2026-10-09T19:00:00")):
    setattr(IntegrityTests, f"test_invalid_version_{number:02d}", invalid_version_test(value))


def bad_lookup_test(value):
    def run(self):
        sink, remote, e = self.make()
        remote.lookup_override = value
        with self.assertRaises(PermanentSinkError):
            sink.upsert(e)
        self.assertEqual(remote.writes, [])
    return run


for number, value in enumerate((
    {}, {"results": None}, {"results": "x"}, {"results": [], "has_more": True},
    {"results": [{"id": "a"}, {"id": "b"}]}, {"results": [{}]},
    {"results": [{"id": "../other"}]}, {"results": [{"id": None}]},
)):
    setattr(IntegrityTests, f"test_bad_lookup_{number:02d}", bad_lookup_test(value))


def archived_test(flag):
    def run(self):
        sink, remote, e = self.make(exists=True)
        remote.page[flag] = True
        with self.assertRaises(VerificationError):
            sink.upsert(e)
        self.assertEqual(remote.writes, [])
    return run


for flag in ("archived", "in_trash", "is_archived"):
    setattr(IntegrityTests, "test_" + flag + "_blocks", archived_test(flag))


if __name__ == "__main__":
    unittest.main()

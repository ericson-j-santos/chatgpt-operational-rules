"""Exclusive portfolio triage checkpoints in the existing TODO PostgreSQL database.

This is a reservation/evidence ledger, not another queue or a Notion writer.
Only trusted producers may supply independently verified GitHub/Notion snapshots.
"""
import hashlib
import json
import re
from contextlib import contextmanager
from datetime import datetime
from typing import Any

OPERATION = "portfolio.triage.v1"
NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
IDENTITY = re.compile(r"[A-Za-z0-9_.:-]{3,128}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
MAX_HISTORY = 1000


class ReservationConflict(ValueError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def integer(value: Any, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid bounded integer")
    return value


def identity(value: Any) -> str:
    if not isinstance(value, str) or not IDENTITY.fullmatch(value):
        raise ValueError("invalid request identity")
    return value


def repository(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 200 or not NAME.fullmatch(value):
        raise ValueError("repository must be owner/repo")
    if any(part in {".", ".."} for part in value.split("/")):
        raise ValueError("invalid repository identity")
    return value.lower()


def names(values: Any) -> list[str]:
    if not isinstance(values, list) or len(values) > 1000:
        raise ValueError("repository list must contain at most 1000 entries")
    result = [repository(value) for value in values]
    if len(set(result)) != len(result):
        raise ValueError("duplicate normalized repository identity")
    return sorted(result)


def source_version(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("source version requires a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid source version") from exc
    if parsed.tzinfo is None:
        raise ValueError("source version requires timezone")
    return value


def scope_for(todo_key: Any) -> str:
    if not isinstance(todo_key, str) or not 3 <= len(todo_key) <= 256:
        raise ValueError("invalid canonical TODO key")
    if any(ord(c) < 32 for c in todo_key) or todo_key != todo_key.strip():
        raise ValueError("invalid canonical TODO key")
    return digest([todo_key, OPERATION])


def validate_record(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"repository", "head_sha", "branch"}:
        raise ValueError("record requires repository, head_sha and branch only")
    repo = repository(value["repository"])
    if not isinstance(value["head_sha"], str) or not SHA.fullmatch(value["head_sha"]):
        raise ValueError("record requires full GitHub HEAD SHA")
    branch = value["branch"]
    if (not isinstance(branch, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", branch)
            or ".." in branch or "//" in branch or branch.endswith(("/", ".", ".lock"))):
        raise ValueError("invalid branch")
    return {"repository": repo, "head_sha": value["head_sha"], "branch": branch}


def initialize_schema(queue: Any) -> None:
    """Explicit additive migration. Never called automatically by HTTP requests."""
    with queue._connect() as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout = '2s'")
        cur.execute("SET LOCAL statement_timeout = '5s'")
        cur.execute("CREATE SCHEMA IF NOT EXISTS todo_bus")
        cur.execute("""CREATE TABLE IF NOT EXISTS todo_bus.triage_reservations (
            scope_key text PRIMARY KEY CHECK (length(scope_key) = 64),
            todo_key text NOT NULL,
            checkpoint jsonb NOT NULL CHECK (jsonb_typeof(checkpoint) = 'object')
        )""")


class TriageReservations:
    def __init__(self, queue: Any):
        self.queue = queue

    @contextmanager
    def _locked(self, todo_key: str, seed: dict | None = None):
        scope = scope_for(todo_key)
        with self.queue._connect() as conn, conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '2s'")
            cur.execute("SET LOCAL statement_timeout = '5s'")
            if seed is not None:
                cur.execute("""INSERT INTO todo_bus.triage_reservations(scope_key,todo_key,checkpoint)
                    VALUES (%s,%s,%s::jsonb) ON CONFLICT (scope_key) DO NOTHING""",
                    (scope, todo_key, canonical(seed)))
            cur.execute("SELECT checkpoint FROM todo_bus.triage_reservations WHERE scope_key=%s FOR UPDATE", (scope,))
            row = cur.fetchone()
            if row is None:
                raise ReservationConflict("unknown scope; claim from a verified snapshot first")
            # Read the database clock AFTER obtaining the row lock.
            cur.execute("SELECT extract(epoch FROM clock_timestamp())")
            now = float(cur.fetchone()[0])
            state = row[0]
            yield cur, scope, state, now

    @staticmethod
    def _save(cur: Any, scope: str, state: dict, expected_version: int) -> None:
        cur.execute("""UPDATE todo_bus.triage_reservations SET checkpoint=%s::jsonb
            WHERE scope_key=%s AND (checkpoint->>'version')::bigint=%s""",
            (canonical(state), scope, expected_version))
        if cur.rowcount != 1:
            raise ReservationConflict("checkpoint changed; refresh snapshot")

    @staticmethod
    def _summary(state: dict) -> dict:
        reviewed = sorted(set(state["known"]) | set(state["records"]))
        return {"version": state["version"], "fence": state["fence"],
                "source_version": state["source_version"], "reviewed": reviewed,
                "known_baseline_count": len(state["known"]),
                "accepted_record_count": len(state["records"]), "unique_count": len(reviewed),
                "pending": sorted(set(state["inventory"]) - set(reviewed)),
                "active_claim": state["active"], "todo_completed": False,
                "external_projection": "PENDING"}

    def snapshot(self, todo_key: str) -> dict:
        scope = scope_for(todo_key)
        with self.queue._connect() as conn, conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '5s'")
            cur.execute("SELECT checkpoint FROM todo_bus.triage_reservations WHERE scope_key=%s", (scope,))
            row = cur.fetchone()
        if row is None:
            return {"version": 0, "initialized": False, "todo_completed": False}
        return {"initialized": True, **self._summary(row[0])}

    def claim(self, body: dict) -> dict:
        required = {"todo_key", "claim_id", "correlation_id", "expected_version", "source_version", "inventory", "known"}
        if not required <= set(body) or set(body) - required - {"lease_seconds", "limit"}:
            raise ValueError("invalid claim fields")
        key = body["todo_key"]
        scope_for(key)
        claim_id, correlation = identity(body["claim_id"]), identity(body["correlation_id"])
        version = integer(body["expected_version"], 0, 1000000)
        ttl = integer(body.get("lease_seconds", 300), 1, 300)
        limit = integer(body.get("limit", 10), 1, 100)
        inventory, known = names(body["inventory"]), names(body["known"])
        if not set(known) <= set(inventory):
            raise ValueError("known repositories must belong to inventory")
        source = source_version(body["source_version"])
        fingerprint = digest(body)
        seed = {"version": 0, "fence": 0, "active": None, "inventory": inventory,
                "known": known, "source_version": source, "records": {}, "claims": {}, "receipts": {}}
        with self._locked(key, seed) as (cur, scope, state, now):
            if (state["source_version"] != source or state["inventory"] != inventory or state["known"] != known):
                raise ReservationConflict("source snapshot changed; reconcile before claiming")
            old = state["claims"].get(claim_id)
            if old:
                if old["fingerprint"] != fingerprint:
                    raise ReservationConflict("claim identity reused with a different payload")
                if state["active"] == claim_id and old["lease_until"] > now:
                    return {**old["receipt"], "replayed": True}
                raise ReservationConflict("expired or finished claim cannot be renewed by replay")
            if state["version"] != version:
                raise ReservationConflict("checkpoint version conflict")
            active = state["claims"].get(state["active"])
            if active and active["lease_until"] > now:
                raise ReservationConflict("scope already reserved")
            if len(state["claims"]) >= MAX_HISTORY:
                raise ReservationConflict("claim history limit reached; reconcile explicitly")
            pending = sorted(set(inventory) - set(known) - set(state["records"]))[:limit]
            if not pending:
                return {"state": "NO_WORK", "version": version, "todo_completed": False}
            state["fence"] += 1
            receipt = {"state": "RESERVED", "claim_id": claim_id, "fence": state["fence"],
                       "version": version, "candidates": pending, "lease_until": now + ttl,
                       "correlation_id": correlation, "todo_completed": False}
            state["claims"][claim_id] = {"fingerprint": fingerprint, "lease_until": now + ttl, "receipt": receipt}
            state["active"] = claim_id
            self._save(cur, scope, state, version)
            return {**receipt, "replayed": False}

    @staticmethod
    def _guard(state: dict, body: dict, now: float) -> None:
        if (state["active"] != body["claim_id"] or state["fence"] != body["fence"]
                or state["version"] != body["expected_version"]):
            raise ReservationConflict("stale owner, fencing token or checkpoint version")
        if state["claims"][body["claim_id"]]["lease_until"] <= now:
            raise ReservationConflict("lease expired; old worker is fenced")

    def complete(self, body: dict) -> dict:
        required = {"todo_key", "claim_id", "fence", "expected_version", "source_version", "commit_id", "records"}
        if set(body) != required:
            raise ValueError("invalid complete fields")
        scope_for(body["todo_key"])
        identity(body["claim_id"])
        commit_id = identity(body["commit_id"])
        integer(body["fence"], 1, 1000000)
        version = integer(body["expected_version"], 0, 1000000)
        source_version(body["source_version"])
        if not isinstance(body["records"], list) or not 1 <= len(body["records"]) <= 100:
            raise ValueError("complete requires 1..100 records")
        records = [validate_record(value) for value in body["records"]]
        if len({record["repository"] for record in records}) != len(records):
            raise ValueError("duplicate repository in completion")
        fingerprint = digest(body)
        with self._locked(body["todo_key"]) as (cur, scope, state, now):
            previous = state["receipts"].get(commit_id)
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise ReservationConflict("commit identity reused with different evidence")
                return {**previous["receipt"], "replayed": True}
            self._guard(state, body, now)
            if state["source_version"] != body["source_version"]:
                raise ReservationConflict("source version conflict")
            candidates = set(state["claims"][body["claim_id"]]["receipt"]["candidates"])
            if any(record["repository"] not in candidates or record["repository"] in state["records"] for record in records):
                raise ReservationConflict("evidence outside reserved scope or already committed")
            if len(state["receipts"]) >= MAX_HISTORY:
                raise ReservationConflict("receipt history limit reached")
            for record in records:
                state["records"][record["repository"]] = {**record, "commit_id": commit_id}
            state["version"] += 1
            state["active"] = None
            receipt = {"state": "CHECKPOINT_PERSISTED", "version": state["version"],
                       "unique_count": len(set(state["known"]) | set(state["records"])),
                       "added": sorted(record["repository"] for record in records),
                       "todo_completed": False, "external_projection": "PENDING"}
            state["receipts"][commit_id] = {"fingerprint": fingerprint, "receipt": receipt}
            self._save(cur, scope, state, version)
            return {**receipt, "replayed": False}

    def release(self, body: dict) -> dict:
        if set(body) != {"todo_key", "claim_id", "fence", "expected_version"}:
            raise ValueError("invalid release fields")
        scope_for(body["todo_key"])
        identity(body["claim_id"])
        integer(body["fence"], 1, 1000000)
        version = integer(body["expected_version"], 0, 1000000)
        with self._locked(body["todo_key"]) as (cur, scope, state, now):
            if (state["active"] is None and state["fence"] == body["fence"]
                    and state["version"] == version and body["claim_id"] in state["claims"]
                    and state["claims"][body["claim_id"]]["receipt"]["fence"] == body["fence"]):
                return {"state": "RELEASED", "replayed": True}
            self._guard(state, body, now)
            state["active"] = None
            self._save(cur, scope, state, version)
            return {"state": "RELEASED", "replayed": False}


def install_routes(app: Any, queue: Any, gateway_token: str) -> None:
    from fastapi import Header, HTTPException, Request
    from starlette.concurrency import run_in_threadpool
    from services.todo_gateway.gateway import _require_authorized, json_loads, MAX_BODY_BYTES

    reservations = TriageReservations(queue)

    @app.get("/v1/triage/snapshot")
    def snapshot(todo_key: str, authorization: str | None = Header(default=None)):
        _require_authorized(gateway_token, authorization)
        try:
            return reservations.snapshot(todo_key)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(503, "reservation store unavailable") from exc

    @app.post("/v1/triage/{action}")
    async def mutate(action: str, request: Request, authorization: str | None = Header(default=None)):
        _require_authorized(gateway_token, authorization)
        if action not in {"claim", "complete", "release"}:
            raise HTTPException(404, "unknown reservation operation")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_BODY_BYTES:
                raise HTTPException(413, "payload too large")
        try:
            body = json_loads(bytes(raw))
            return await run_in_threadpool(getattr(reservations, action), body)
        except ReservationConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            raise HTTPException(422, "invalid reservation request") from exc
        except Exception as exc:
            raise HTTPException(503, "reservation store unavailable") from exc

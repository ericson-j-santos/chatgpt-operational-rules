from __future__ import annotations

import hmac
import logging
import uuid
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from scripts.todo_event_bus import TodoEvent, VALID_STATUSES

LOGGER = logging.getLogger("todo_gateway")
MAX_BODY_BYTES = 65536


def _authorized(expected_token: str, authorization: str | None) -> bool:
    if not expected_token or not authorization or not authorization.startswith("Bearer "):
        return False
    supplied = authorization[7:]
    return hmac.compare_digest(expected_token.encode(), supplied.encode())


def _require_authorized(expected_token: str, authorization: str | None) -> None:
    if not _authorized(expected_token, authorization):
        raise HTTPException(status_code=401, detail="unauthorized")


def _validate_operational_semantics(event: TodoEvent) -> None:
    todo = event.todo
    status = todo.get("status")
    if status == "CONCLUÍDO":
        if not str(todo.get("completion_criteria") or "").strip():
            raise ValueError("CONCLUÍDO exige completion_criteria")
        if not str(todo.get("evidence") or "").strip():
            raise ValueError("CONCLUÍDO exige evidence")
    if status == "BLOQUEADO":
        if not str(todo.get("blocker") or "").strip():
            raise ValueError("BLOQUEADO exige blocker")
        if not str(todo.get("next_action") or "").strip():
            raise ValueError("BLOQUEADO exige next_action")


def create_app(queue: Any, gateway_token: str) -> FastAPI:
    if not gateway_token:
        raise ValueError("gateway token must be configured")

    app = FastAPI(title="TODO Global Event Gateway", version="1.1.0")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> dict[str, str]:
        try:
            ready = bool(queue.ready())
        except Exception:
            LOGGER.exception("queue readiness failed")
            raise HTTPException(status_code=503, detail="queue unavailable")
        if not ready:
            raise HTTPException(status_code=503, detail="queue unavailable")
        return {"status": "ready"}

    @app.post("/v1/events", status_code=202)
    async def publish_event(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        _require_authorized(gateway_token, authorization)

        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_BODY_BYTES:
                    raise HTTPException(status_code=413, detail="payload too large")
            except ValueError:
                raise HTTPException(status_code=400, detail="invalid content-length")

        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="payload too large")

        try:
            body = json_loads(raw)
            event = TodoEvent.from_dict(body)
            _validate_operational_semantics(event)
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=422, detail=f"invalid TodoEvent: {exc}") from exc

        try:
            inserted = bool(queue.enqueue(event))
        except Exception:
            LOGGER.exception(
                "event persistence failed",
                extra={"event_id": event.event_id, "correlation_id": event.correlation_id},
            )
            raise HTTPException(status_code=503, detail="queue unavailable")

        return JSONResponse(
            status_code=202,
            content={
                "accepted": True,
                "duplicate": not inserted,
                "event_id": event.event_id,
                "correlation_id": event.correlation_id,
            },
        )

    @app.get("/v1/todos")
    def list_todos(
        authorization: str | None = Header(default=None),
        status: str | None = Query(default=None),
        project: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=200),
    ) -> dict[str, Any]:
        _require_authorized(gateway_token, authorization)
        if status is not None and status not in VALID_STATUSES:
            raise HTTPException(status_code=422, detail="invalid status")
        try:
            items = list(queue.list_latest_todos(limit=limit))
        except Exception:
            LOGGER.exception("control-plane TODO read failed")
            raise HTTPException(status_code=503, detail="queue unavailable")

        if status is not None:
            items = [item for item in items if item.get("todo", {}).get("status") == status]
        if project is not None:
            expected = project.strip().casefold()
            items = [item for item in items if str(item.get("project") or "").strip().casefold() == expected]
        return {"items": items, "count": len(items)}

    @app.get("/v1/continuations")
    def list_continuations(
        authorization: str | None = Header(default=None),
        limit: int = Query(default=100, ge=1, le=200),
    ) -> dict[str, Any]:
        _require_authorized(gateway_token, authorization)
        try:
            items = list(queue.list_continuation_requests(limit=limit))
        except Exception:
            LOGGER.exception("continuation queue read failed")
            raise HTTPException(status_code=503, detail="queue unavailable")
        return {"items": items, "count": len(items)}

    @app.post("/v1/todos/{idempotency_key}/continue", status_code=202)
    async def request_continuation(
        idempotency_key: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        _require_authorized(gateway_token, authorization)
        correlation_id = f"continue-{uuid.uuid4().hex}"
        raw = await request.body()
        if raw:
            if len(raw) > MAX_BODY_BYTES:
                raise HTTPException(status_code=413, detail="payload too large")
            try:
                body = json_loads(raw)
            except (ValueError, TypeError, UnicodeDecodeError) as exc:
                raise HTTPException(status_code=422, detail=f"invalid request: {exc}") from exc
            supplied = str(body.get("correlation_id") or "").strip()
            if supplied:
                correlation_id = supplied

        try:
            result = queue.request_continuation(idempotency_key, correlation_id)
        except ValueError as exc:
            message = str(exc)
            if message.startswith("terminal TODO"):
                raise HTTPException(status_code=409, detail=message) from exc
            raise HTTPException(status_code=422, detail=message) from exc
        except Exception:
            LOGGER.exception(
                "continuation persistence failed",
                extra={"idempotency_key": idempotency_key, "correlation_id": correlation_id},
            )
            raise HTTPException(status_code=503, detail="queue unavailable")

        if result is None:
            raise HTTPException(status_code=404, detail="TODO not found")
        return JSONResponse(
            status_code=202,
            content={
                "accepted": True,
                "duplicate": bool(result.get("duplicate")),
                "request_id": result["request_id"],
                "idempotency_key": result["idempotency_key"],
                "basis_event_id": result["basis_event_id"],
                "correlation_id": result["correlation_id"],
                "state": result["state"],
            },
        )

    return app


def json_loads(raw: bytes) -> dict[str, Any]:
    import json

    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("body must be an object")
    return data


def create_app_from_env() -> FastAPI:
    import os

    from services.todo_gateway.repository import PostgresQueueRepository

    token = os.environ.get("TODO_GATEWAY_TOKEN", "")
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    queue = PostgresQueueRepository(database_url)
    return create_app(queue, token)

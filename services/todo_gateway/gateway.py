from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from scripts.todo_event_bus import TodoEvent

LOGGER = logging.getLogger("todo_gateway")
MAX_BODY_BYTES = 65536


def _authorized(expected_token: str, authorization: str | None) -> bool:
    if not expected_token or not authorization or not authorization.startswith("Bearer "):
        return False
    supplied = authorization[7:]
    return hmac.compare_digest(expected_token.encode(), supplied.encode())


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

    app = FastAPI(title="TODO Global Event Gateway", version="1.0.0")

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
        if not _authorized(gateway_token, authorization):
            raise HTTPException(status_code=401, detail="unauthorized")

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

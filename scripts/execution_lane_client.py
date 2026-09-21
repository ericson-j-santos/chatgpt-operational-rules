#!/usr/bin/env python3
"""Cliente fail-closed para despachar TODOs tipados a uma lane de execução."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
ALLOWED_FIELDS = {
    "repository",
    "issue_number",
    "request_id",
    "base_sha",
    "target_branch",
    "priority",
    "max_attempts",
}


class ExecutionLaneError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExecutionLaneClient:
    base_url: str
    token_file: Path
    timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", validate_base_url(self.base_url))
        if self.timeout_seconds <= 0 or self.timeout_seconds > 60:
            raise ValueError("timeout_seconds inválido")

    def _token(self) -> str:
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ExecutionLaneError("execution_lane_token_unavailable") from exc
        if not token:
            raise ExecutionLaneError("execution_lane_token_empty")
        return token

    def enqueue(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = validate_execution_request(payload)
        body = json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode("utf-8")
        request = Request(
            self.base_url + "/v1/tasks",
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._token()}",
                "User-Agent": "todo-execution-lane-dispatch/1",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                status = int(getattr(response, "status", response.getcode()))
                raw = response.read(262144)
        except HTTPError as exc:
            raise ExecutionLaneError(f"execution_lane_http_{exc.code}") from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise ExecutionLaneError(f"execution_lane_unavailable:{type(exc).__name__}") from exc
        if status not in (200, 201):
            raise ExecutionLaneError(f"execution_lane_http_{status}")
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExecutionLaneError("execution_lane_invalid_json") from exc
        if not isinstance(data, dict) or not isinstance(data.get("task"), dict):
            raise ExecutionLaneError("execution_lane_invalid_response")
        task = data["task"]
        for field in ("repository", "issue_number", "request_id"):
            if task.get(field) != normalized[field]:
                raise ExecutionLaneError(f"execution_lane_response_mismatch:{field}")
        return data


def validate_base_url(value: str) -> str:
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        raise ValueError("execution lane base URL ausente")
    parsed = urlsplit(raw)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("execution lane base URL inválida")
    if parsed.path not in ("", "/"):
        raise ValueError("execution lane base URL deve apontar para a raiz")
    host = (parsed.hostname or "").casefold()
    if parsed.scheme == "http":
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("HTTP permitido somente em loopback")
    elif parsed.scheme != "https":
        raise ValueError("execution lane exige HTTPS ou HTTP em loopback")
    if not host or parsed.port is None:
        raise ValueError("execution lane exige host e porta explícitos")
    return raw


def validate_execution_request(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("execution_request deve ser objeto")
    unknown = sorted(set(payload) - ALLOWED_FIELDS)
    if unknown:
        raise ValueError("execution_request contém campos não permitidos: " + ", ".join(unknown))

    repository = str(payload.get("repository") or "").strip()
    if not REPOSITORY_RE.fullmatch(repository):
        raise ValueError("execution_request.repository inválido")
    issue_number = payload.get("issue_number")
    if isinstance(issue_number, bool) or not isinstance(issue_number, int) or issue_number < 1:
        raise ValueError("execution_request.issue_number inválido")
    request_id = str(payload.get("request_id") or "").strip()
    if not (1 <= len(request_id) <= 256):
        raise ValueError("execution_request.request_id inválido")
    base_sha = str(payload.get("base_sha") or "").strip().lower()
    if not SHA40_RE.fullmatch(base_sha):
        raise ValueError("execution_request.base_sha inválido")

    result: dict[str, Any] = {
        "repository": repository,
        "issue_number": issue_number,
        "request_id": request_id,
        "base_sha": base_sha,
    }
    target_branch = payload.get("target_branch")
    if target_branch is not None:
        target_branch = str(target_branch).strip()
        if not target_branch or len(target_branch) > 200:
            raise ValueError("execution_request.target_branch inválido")
        result["target_branch"] = target_branch

    priority = payload.get("priority")
    if priority is not None:
        if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 10000:
            raise ValueError("execution_request.priority inválido")
        result["priority"] = priority

    max_attempts = payload.get("max_attempts")
    if max_attempts is not None:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 20:
            raise ValueError("execution_request.max_attempts inválido")
        result["max_attempts"] = max_attempts
    return result

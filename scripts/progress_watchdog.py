#!/usr/bin/env python3
"""Decide de forma reproduzível se uma execução está progredindo ou estagnada."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_STALL_AFTER_SECONDS = 300
TERMINAL_STATES = {"completed", "failed", "cancelled", "blocked"}


def _parse_timestamp(value: Any, field: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{field} é obrigatório")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} deve ser ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} deve possuir timezone")
    return parsed.astimezone(timezone.utc)


def _positive_int(value: Any, field: str, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} deve ser inteiro positivo") from exc
    if parsed < 1:
        raise ValueError(f"{field} deve ser inteiro positivo")
    return parsed


def evaluate(payload: dict[str, Any]) -> dict[str, Any]:
    task_id = str(payload.get("task_id") or "").strip()
    correlation_id = str(payload.get("correlation_id") or "").strip()
    state = str(payload.get("state") or "").strip().lower()
    if not task_id:
        raise ValueError("task_id é obrigatório")
    if not correlation_id:
        raise ValueError("correlation_id é obrigatório")
    if not state:
        raise ValueError("state é obrigatório")

    now = _parse_timestamp(payload.get("now_at") or datetime.now(timezone.utc).isoformat(), "now_at")
    progress_at = _parse_timestamp(payload.get("last_material_progress_at"), "last_material_progress_at")
    if progress_at > now:
        raise ValueError("last_material_progress_at não pode estar no futuro")

    observation_at = None
    if payload.get("last_observation_at"):
        observation_at = _parse_timestamp(payload.get("last_observation_at"), "last_observation_at")
        if observation_at > now:
            raise ValueError("last_observation_at não pode estar no futuro")

    threshold = _positive_int(
        payload.get("stall_after_seconds"),
        "stall_after_seconds",
        DEFAULT_STALL_AFTER_SECONDS,
    )
    no_progress_seconds = int((now - progress_at).total_seconds())
    observation_age_seconds = (
        int((now - observation_at).total_seconds()) if observation_at is not None else None
    )

    if state in TERMINAL_STATES:
        decision = "terminal"
        stalled = False
        reason = "terminal_state"
    elif no_progress_seconds < threshold:
        decision = "continue"
        stalled = False
        reason = "within_progress_window"
    elif payload.get("alternative_route_available") is True:
        decision = "switch_route"
        stalled = True
        reason = "material_progress_timeout_alternative_available"
    else:
        decision = "block"
        stalled = True
        reason = "material_progress_timeout_no_alternative"

    return {
        "task_id": task_id,
        "correlation_id": correlation_id,
        "state": state,
        "stalled": stalled,
        "decision": decision,
        "reason_code": reason,
        "stall_after_seconds": threshold,
        "last_material_progress_at": progress_at.isoformat(),
        "last_observation_at": observation_at.isoformat() if observation_at else None,
        "no_progress_seconds": no_progress_seconds,
        "observation_age_seconds": observation_age_seconds,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Watchdog de progresso material")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        result = evaluate(payload)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(json.dumps({"ready": False, "error": str(exc)}, ensure_ascii=False))
        return 2

    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True)
    print(rendered)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8", newline="\n")

    if result["decision"] == "switch_route":
        return 3
    if result["decision"] == "block":
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

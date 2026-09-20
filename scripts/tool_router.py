#!/usr/bin/env python3
"""Roteia tarefas para o menor executor adequado, considerando cota/custo."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

REMOTE_RESERVE_THRESHOLD = 20

DIRECT_CONNECTORS = {
    "github": "github_api",
    "gitlab": "gitlab_api",
    "google_drive": "google_drive",
    "gmail": "gmail",
    "calendar": "google_calendar",
    "notion": "notion",
}

LOCAL_TYPES = {"local_machine", "local_command", "local_files", "local_process", "host_recovery"}


def _bool(capabilities: dict[str, Any], name: str) -> bool:
    return capabilities.get(name) is True


def _pct(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("remote_calls_left_pct deve ser inteiro 0..100") from exc
    if not 0 <= parsed <= 100:
        raise ValueError("remote_calls_left_pct deve estar entre 0 e 100")
    return parsed


def _balance(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("tinyfish_balance_usd deve ser numérico") from exc


def evaluate(payload: dict[str, Any]) -> dict[str, Any]:
    task_type = str(payload.get("task_type") or "").strip().lower()
    if not task_type:
        raise ValueError("task_type é obrigatório")

    capabilities = payload.get("capabilities") or {}
    if not isinstance(capabilities, dict):
        raise ValueError("capabilities deve ser objeto")

    remote_pct = _pct(payload.get("remote_calls_left_pct"))
    tiny_balance = _balance(payload.get("tinyfish_balance_usd"))
    reserve_mode = remote_pct is not None and remote_pct <= REMOTE_RESERVE_THRESHOLD
    reasons: list[str] = []
    avoided: list[str] = []

    connector = DIRECT_CONNECTORS.get(task_type)
    if connector:
        if _bool(capabilities, connector):
            selected = connector
            reasons.append("native_connector_available")
        else:
            selected = None
            reasons.append("native_connector_unavailable")
            avoided.extend(["tinyfish", "remote_desktop"])
    elif task_type in {"web_research", "public_web"}:
        if _bool(capabilities, "native_web"):
            selected = "native_web"
            reasons.append("native_web_preferred")
            avoided.extend(["tinyfish", "remote_desktop"])
        elif _bool(capabilities, "tinyfish") and tiny_balance is not None and tiny_balance > 0:
            selected = "tinyfish"
            reasons.append("tinyfish_contingency_positive_balance")
            avoided.append("remote_desktop")
        else:
            selected = None
            reasons.append("no_web_executor_with_budget")
    elif task_type in {"browser_automation", "web_interaction"}:
        if _bool(capabilities, "cloud_browser"):
            selected = "cloud_browser"
            reasons.append("native_cloud_browser_preferred")
            avoided.extend(["tinyfish", "remote_desktop"])
        elif _bool(capabilities, "tinyfish") and tiny_balance is not None and tiny_balance > 0:
            selected = "tinyfish"
            reasons.append("tinyfish_browser_contingency_positive_balance")
            avoided.append("remote_desktop")
        else:
            selected = None
            reasons.append("browser_automation_unavailable_or_no_budget")
    elif task_type in LOCAL_TYPES:
        if _bool(capabilities, "remote_desktop") and payload.get("remote_controller_online") is True:
            selected = "remote_desktop"
            reasons.append("task_intrinsically_local")
            if reserve_mode:
                reasons.append("remote_reserve_mode_allowed_for_local_task")
        else:
            selected = None
            reasons.append("remote_or_controller_unavailable")
    else:
        explicit = str(payload.get("preferred_native_executor") or "").strip()
        if explicit and _bool(capabilities, explicit):
            selected = explicit
            reasons.append("explicit_native_executor_available")
        else:
            selected = None
            reasons.append("no_safe_route")

    if selected != "tinyfish":
        if tiny_balance is not None and tiny_balance <= 0:
            avoided.append("tinyfish_balance_nonpositive")
    if selected != "remote_desktop":
        if reserve_mode:
            avoided.append("remote_desktop_reserve_mode")
        elif _bool(capabilities, "remote_desktop"):
            avoided.append("remote_desktop_not_required")

    return {
        "ready": selected is not None,
        "task_type": task_type,
        "selected_executor": selected,
        "reserve_mode": reserve_mode,
        "remote_calls_left_pct": remote_pct,
        "tinyfish_balance_usd": tiny_balance,
        "reasons": sorted(set(reasons)),
        "avoided": sorted(set(avoided)),
        "next_step": None if selected is not None else (
            "dual_host_preflight_or_controller_recovery"
            if task_type in LOCAL_TYPES
            else "provision_or_authorize_native_executor"
        ),
        "correlation_id": payload.get("correlation_id"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Roteamento Pareto de ferramentas")
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
    return 0 if result["ready"] else 3


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Roteia tarefas para o menor executor adequado, considerando cota/custo."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
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
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("remote_calls_left_pct deve ser inteiro 0..100")
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



def _instant(value: Any) -> datetime | None:
    """Aceita somente data/hora ISO 8601 com fuso explícito."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _remote_quota_block_reason(payload: dict[str, Any], pct: int | None) -> str | None:
    """Avalia evidência fornecida pelo chamador; não consulta nem consome RDC."""
    blocked = payload.get("remote_quota_blocked", False)
    if not isinstance(blocked, bool):
        raise ValueError("remote_quota_blocked deve ser booleano")
    if pct is None:
        return "remote_quota_unknown"
    if pct == 0:
        return "remote_quota_exhausted"
    if not blocked:
        return None

    # Um saldo positivo ou a virada do mês não apaga bloqueio anterior.
    source = payload.get("quota_renewal_source")
    if payload.get("remote_quota_renewal_confirmed") is not True:
        return "remote_quota_renewal_unproven"
    if not isinstance(source, str) or not source.strip():
        return "remote_quota_renewal_unproven"
    blocked_at = _instant(payload.get("quota_blocked_at"))
    renewed_at = _instant(payload.get("quota_renewed_at"))
    observed_at = _instant(payload.get("quota_observed_at"))
    if blocked_at is None or renewed_at is None or observed_at is None:
        return "remote_quota_renewal_unproven"
    if not blocked_at < renewed_at <= observed_at <= datetime.now(timezone.utc):
        return "remote_quota_renewal_unproven"

    reset_value = payload.get("reset_at")
    if reset_value not in (None, "unknown"):
        reset_at = _instant(reset_value)
        if reset_at is None or reset_at > renewed_at:
            return "remote_quota_reset_not_reached"
    return None

def evaluate(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("entrada deve ser objeto JSON")
    task_type = str(payload.get("task_type") or "").strip().lower()
    if not task_type:
        raise ValueError("task_type é obrigatório")

    capabilities = payload.get("capabilities") or {}
    if not isinstance(capabilities, dict):
        raise ValueError("capabilities deve ser objeto")

    remote_pct = _pct(payload.get("remote_calls_left_pct"))
    tiny_balance = _balance(payload.get("tinyfish_balance_usd"))
    quota_reason = _remote_quota_block_reason(payload, remote_pct)
    quota_blocked = remote_pct == 0 or (
        payload.get("remote_quota_blocked") is True and quota_reason is not None
    )
    reserve_mode = remote_pct is not None and 0 < remote_pct <= REMOTE_RESERVE_THRESHOLD
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
        remote_available = _bool(capabilities, "remote_desktop")
        controller_online = payload.get("remote_controller_online") is True
        semantic_ok = payload.get("remote_controller_semantic_ok") is True
        if quota_reason is not None:
            selected = None
            reasons.append(quota_reason)
            avoided.append("remote_desktop")
        elif remote_available and controller_online and semantic_ok:
            selected = "remote_desktop"
            reasons.append("task_intrinsically_local")
            reasons.append("remote_controller_semantic_ok")
            if reserve_mode:
                reasons.append("remote_reserve_mode_allowed_for_local_task")
        else:
            selected = None
            if remote_available and controller_online and not semantic_ok:
                reasons.append("remote_controller_semantic_unproven")
            else:
                reasons.append("remote_or_controller_unavailable")
    else:
        explicit = str(payload.get("preferred_native_executor") or "").strip()
        if explicit == "remote_desktop":
            selected = None
            reasons.append("remote_requires_intrinsically_local_task")
            avoided.append("remote_desktop")
        elif explicit and _bool(capabilities, explicit):
            selected = explicit
            reasons.append("explicit_native_executor_available")
        else:
            selected = None
            reasons.append("no_safe_route")

    if selected != "tinyfish":
        if tiny_balance is not None and tiny_balance <= 0:
            avoided.append("tinyfish_balance_nonpositive")
    if selected != "remote_desktop":
        if quota_reason is not None:
            avoided.append(quota_reason)
        elif reserve_mode:
            avoided.append("remote_desktop_reserve_mode")
        elif _bool(capabilities, "remote_desktop"):
            avoided.append("remote_desktop_not_required")

    return {
        "ready": selected is not None,
        "task_type": task_type,
        "selected_executor": selected,
        "reserve_mode": reserve_mode,
        "remote_calls_left_pct": remote_pct,
        "remote_quota_blocked": quota_blocked,
        "remote_quota_block_reason": quota_reason,
        "quota_blocked_at": payload.get("quota_blocked_at") or (
            payload.get("quota_observed_at") if remote_pct == 0 else None
        ),
        "quota_observed_at": payload.get("quota_observed_at"),
        "quota_renewed_at": payload.get("quota_renewed_at"),
        "quota_renewal_source": payload.get("quota_renewal_source"),
        "reset_at": payload.get("reset_at"),
        "tinyfish_balance_usd": tiny_balance,
        "reasons": sorted(set(reasons)),
        "avoided": sorted(set(avoided)),
        "next_step": None if selected is not None else (
            "use_non_rdc_executor_until_quota_verified"
            if task_type in LOCAL_TYPES and quota_reason is not None
            else "dual_host_preflight_or_controller_recovery"
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
        exit_code = 0 if result["ready"] else 3
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        result = {"ready": False, "selected_executor": None, "error": str(exc)}
        exit_code = 2
    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True)
    try:
        if args.output:
            args.output.write_text(rendered + "\n", encoding="utf-8", newline="\n")
    except OSError:
        print(json.dumps({"ready": False, "error": "não foi possível gravar a decisão"}, ensure_ascii=False))
        return 2
    print(rendered)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Roteia tarefas para o menor executor adequado, considerando cota/custo."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REMOTE_RESERVE_THRESHOLD = 20
REMOTE_QUOTA_OBSERVATION_MAX_AGE = timedelta(minutes=15)

REMOTE_EXECUTOR_IDENTITIES = {
    "rdc",
    "remotedesktop",
    "remotedesktopcommander",
    "remotemcp",
}

DIRECT_CONNECTORS = {
    "github": "github_api",
    "gitlab": "gitlab_api",
    "google_drive": "google_drive",
    "gmail": "gmail",
    "calendar": "google_calendar",
    "notion": "notion",
}

SAFE_EXPLICIT_NATIVE_EXECUTORS = {
    *DIRECT_CONNECTORS.values(),
    "native_web",
    "cloud_browser",
}

LOCAL_TYPES = {"local_machine", "local_command", "local_files", "local_process", "host_recovery"}


def _bool(capabilities: dict[str, Any], name: str) -> bool:
    return capabilities.get(name) is True


def _pct(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("remote_calls_left_pct deve ser inteiro menor ou igual a 100")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("remote_calls_left_pct deve ser inteiro menor ou igual a 100") from exc
    if parsed > 100:
        raise ValueError("remote_calls_left_pct deve ser menor ou igual a 100")
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
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (OverflowError, ValueError):
        return None


def _executor_identity(value: Any) -> str:
    """Normaliza aliases para impedir que casing/pontuação disfarcem o RDC."""
    return "".join(character for character in str(value or "").casefold() if character.isalnum())


def _is_remote_executor(value: Any) -> bool:
    """Reconhece famílias RDC; executores explícitos ainda dependem de allowlist."""
    identity = _executor_identity(value)
    return identity in REMOTE_EXECUTOR_IDENTITIES or identity.startswith(
        ("rdc", "remotedesktop", "remotecommander", "remotemcp")
    )


def _newest_timestamp_value(
    incoming: Any,
    persisted: Any,
    *,
    past_only: bool,
) -> Any:
    """Conserva o timestamp válido mais novo para impedir rollback do checkpoint."""
    now = datetime.now(timezone.utc)
    parsed: list[tuple[datetime, Any]] = []
    for raw in (persisted, incoming):
        instant = _instant(raw)
        if instant is not None and (not past_only or instant <= now):
            parsed.append((instant, raw))
    if parsed:
        return max(parsed, key=lambda item: item[0])[1]
    if persisted not in (None, ""):
        return persisted
    return incoming


def _quota_observation_is_current(payload: dict[str, Any]) -> bool:
    """Valida proveniência e frescor sem inferir renovação ou reset da conta."""
    source = payload.get("quota_observation_source")
    observed_at = _instant(payload.get("quota_observed_at"))
    if not isinstance(source, str) or not source.strip():
        return False
    now = datetime.now(timezone.utc)
    return not (
        observed_at is None
        or observed_at > now
        or now - observed_at > REMOTE_QUOTA_OBSERVATION_MAX_AGE
    )


def _remote_quota_observation_reason(payload: dict[str, Any], pct: int | None) -> str | None:
    """Exige leitura positiva recente; a janela operacional não é data de reset."""
    if pct is None or pct <= 0:
        return None
    return None if _quota_observation_is_current(payload) else "remote_quota_observation_unproven"


def _remote_quota_block_reason(payload: dict[str, Any], pct: int | None) -> str | None:
    """Avalia evidência fornecida pelo chamador; não consulta nem consome RDC."""
    if pct is None:
        return "remote_quota_unknown"
    if pct <= 0:
        return "remote_quota_exhausted"
    if payload.get("remote_quota_checkpoint_invalid") is True:
        return "remote_quota_checkpoint_invalid"
    if payload.get("remote_quota_checkpoint_incomplete") is True:
        return "remote_quota_checkpoint_incomplete"
    blocked = payload.get("remote_quota_blocked", False)
    if not isinstance(blocked, bool):
        raise ValueError("remote_quota_blocked deve ser booleano")
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
        if reset_at is None or not blocked_at < reset_at <= renewed_at:
            return "remote_quota_reset_not_reached"
    return None


def _load_checkpoint(path: Path | None) -> dict[str, Any]:
    """Lê o último output para que erro/replay não apague um bloqueio conhecido."""
    if path is None or not path.exists():
        return {}
    try:
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "remote_quota_blocked": True,
            "remote_quota_block_reason": "remote_quota_checkpoint_invalid",
            "remote_quota_checkpoint_invalid": True,
        }
    if (
        not isinstance(checkpoint, dict)
        or not isinstance(checkpoint.get("ready"), bool)
        or "selected_executor" not in checkpoint
    ):
        return {
            "remote_quota_blocked": True,
            "remote_quota_block_reason": "remote_quota_checkpoint_invalid",
            "remote_quota_checkpoint_invalid": True,
        }
    return checkpoint


def _carry_blocked_checkpoint(payload: Any, checkpoint: dict[str, Any]) -> Any:
    if not isinstance(payload, dict) or checkpoint.get("remote_quota_blocked") is not True:
        return payload
    carried = dict(payload)
    carried["remote_quota_blocked"] = True
    if checkpoint.get("remote_quota_checkpoint_invalid") is True:
        carried["remote_quota_checkpoint_invalid"] = True
    if checkpoint.get("remote_quota_checkpoint_incomplete") is True:
        carried["remote_quota_checkpoint_incomplete"] = True
        carried["quota_blocked_at"] = None
    else:
        carried["quota_blocked_at"] = _newest_timestamp_value(
            carried.get("quota_blocked_at"),
            checkpoint.get("quota_blocked_at"),
            past_only=True,
        )
    carried["reset_at"] = _newest_timestamp_value(
        carried.get("reset_at"),
        checkpoint.get("reset_at"),
        past_only=False,
    )
    return carried


def _blocked_state(payload: Any, checkpoint: dict[str, Any]) -> dict[str, Any]:
    """Extrai estado monotônico para respostas de erro sem inventar timestamps."""
    current = payload if isinstance(payload, dict) else {}
    previous = checkpoint if checkpoint.get("remote_quota_blocked") is True else {}
    try:
        pct = _pct(current.get("remote_calls_left_pct"))
    except ValueError:
        pct = None
    if pct is not None and pct <= 0:
        observed_at = (
            current.get("quota_observed_at")
            if _quota_observation_is_current(current)
            else None
        )
        blocked_at = current.get("quota_blocked_at") or previous.get("quota_blocked_at")
        blocked_at = _newest_timestamp_value(observed_at, blocked_at, past_only=True)
        if _instant(blocked_at) is None:
            blocked_at = None
        return {
            "remote_quota_blocked": True,
            "remote_quota_block_reason": "remote_quota_exhausted",
            "remote_quota_checkpoint_invalid": (
                previous.get("remote_quota_checkpoint_invalid") is True
            ),
            "remote_quota_checkpoint_incomplete": blocked_at is None,
            "quota_blocked_at": blocked_at,
            "reset_at": current.get("reset_at") or previous.get("reset_at"),
        }
    source = previous or (current if current.get("remote_quota_blocked") is True else {})
    return {
        "remote_quota_blocked": bool(source),
        "remote_quota_block_reason": source.get("remote_quota_block_reason"),
        "remote_quota_checkpoint_invalid": (
            source.get("remote_quota_checkpoint_invalid") is True
        ),
        "remote_quota_checkpoint_incomplete": (
            source.get("remote_quota_checkpoint_incomplete") is True
        ),
        "quota_blocked_at": source.get("quota_blocked_at"),
        "reset_at": source.get("reset_at"),
    }

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
    observation_reason = _remote_quota_observation_reason(payload, remote_pct)
    remote_reason = quota_reason or observation_reason
    quota_blocked = (remote_pct is not None and remote_pct <= 0) or (
        payload.get("remote_quota_blocked") is True and remote_reason is not None
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
        if remote_reason is not None:
            selected = None
            reasons.append(remote_reason)
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
        if _is_remote_executor(explicit):
            selected = None
            reasons.append("remote_requires_intrinsically_local_task")
            avoided.append("remote_desktop")
        elif explicit in SAFE_EXPLICIT_NATIVE_EXECUTORS and _bool(capabilities, explicit):
            selected = explicit
            reasons.append("explicit_native_executor_available")
        elif explicit and _bool(capabilities, explicit):
            selected = None
            reasons.append("explicit_executor_not_allowlisted")
        else:
            selected = None
            reasons.append("no_safe_route")

    if selected != "tinyfish":
        if tiny_balance is not None and tiny_balance <= 0:
            avoided.append("tinyfish_balance_nonpositive")
    if selected != "remote_desktop":
        if remote_reason is not None:
            avoided.append(remote_reason)
        elif reserve_mode:
            avoided.append("remote_desktop_reserve_mode")
        elif _bool(capabilities, "remote_desktop"):
            avoided.append("remote_desktop_not_required")

    raw_observed_at = payload.get("quota_observed_at")
    existing_blocked_at = payload.get("quota_blocked_at")
    previous_blocked = payload.get("remote_quota_blocked") is True
    if remote_pct is not None and remote_pct <= 0:
        current_exhaustion_at = (
            raw_observed_at if _quota_observation_is_current(payload) else None
        )
        quota_blocked_at = _newest_timestamp_value(
            current_exhaustion_at,
            existing_blocked_at if previous_blocked else None,
            past_only=True,
        )
        if _instant(quota_blocked_at) is None:
            quota_blocked_at = None
            reasons.append("remote_quota_checkpoint_incomplete")
    else:
        quota_blocked_at = existing_blocked_at

    checkpoint_incomplete = (
        quota_blocked_at is None
        if remote_pct is not None and remote_pct <= 0
        else payload.get("remote_quota_checkpoint_incomplete") is True
    )

    return {
        "ready": selected is not None,
        "task_type": task_type,
        "selected_executor": selected,
        "reserve_mode": reserve_mode,
        "remote_calls_left_pct": remote_pct,
        "remote_quota_blocked": quota_blocked,
        "remote_quota_block_reason": quota_reason,
        "remote_quota_checkpoint_invalid": (
            payload.get("remote_quota_checkpoint_invalid") is True
        ),
        "remote_quota_checkpoint_incomplete": checkpoint_incomplete,
        "remote_quota_eligibility_reason": remote_reason,
        "quota_blocked_at": quota_blocked_at,
        "quota_observed_at": raw_observed_at,
        "quota_observation_source": payload.get("quota_observation_source"),
        "quota_renewed_at": payload.get("quota_renewed_at"),
        "quota_renewal_source": payload.get("quota_renewal_source"),
        "reset_at": payload.get("reset_at"),
        "tinyfish_balance_usd": tiny_balance,
        "reasons": sorted(set(reasons)),
        "avoided": sorted(set(avoided)),
        "next_step": None if selected is not None else (
            "use_non_rdc_executor_until_quota_verified"
            if task_type in LOCAL_TYPES and remote_reason is not None
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
    checkpoint = _load_checkpoint(args.output)
    payload: Any = None
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        payload = _carry_blocked_checkpoint(payload, checkpoint)
        result = evaluate(payload)
        exit_code = 0 if result["ready"] else 3
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        result = {
            "ready": False,
            "selected_executor": None,
            "error": str(exc),
            **_blocked_state(payload, checkpoint),
        }
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

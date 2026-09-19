#!/usr/bin/env python3
"""One-time owner-authorized remote Windows reboot route.

This route is intentionally separate from the local owner_host_power_once.py.
It supports exactly one remote operation: reboot one explicitly authorized
target host from the current executor host.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import owner_host_power_once as base

AUTHORIZE_CONFIRM = "AUTHORIZE-ONE-TIME-REMOTE-REBOOT"
EXECUTE_CONFIRM = "EXECUTE-ONE-TIME-REMOTE-REBOOT"
REVOKE_CONFIRM = "REVOKE-ONE-TIME-REMOTE-REBOOT"
TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$")
MAX_WINDOW_MINUTES = 15


def default_config_path() -> Path:
    return base.default_state_dir() / "owner-remote-host-power-once.local.json"


def audit_path() -> Path:
    return base.default_state_dir() / "owner-remote-host-power-audit.jsonl"


def append_audit(payload: dict[str, Any]) -> None:
    path = audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.write(fd, raw.encode("utf-8"))
    finally:
        os.close(fd)


def validate_target_host(value: str) -> str:
    target = value.strip()
    if not TARGET_RE.fullmatch(target):
        raise base.HostPowerError("target_host inválido")
    return target


def authorize(
    *,
    action_id: str,
    executor_host: str,
    target_host: str,
    minutes: int,
    confirm: str,
    config_path: Path,
    authorization_ref: str,
) -> dict[str, Any]:
    base.validate_action_id(action_id)
    if confirm != AUTHORIZE_CONFIRM:
        raise base.HostPowerError("confirmação de autorização remota inválida")
    local_host = socket.gethostname()
    if executor_host.casefold() != local_host.casefold():
        raise base.HostPowerError("executor_host não corresponde ao host local")
    target = validate_target_host(target_host)
    if target.casefold() == local_host.casefold():
        raise base.HostPowerError("reboot remoto exige target_host diferente do executor")
    if minutes < 1 or minutes > MAX_WINDOW_MINUTES:
        raise base.HostPowerError(
            f"janela deve estar entre 1 e {MAX_WINDOW_MINUTES} minutos"
        )
    if not authorization_ref.strip():
        raise base.HostPowerError("authorization_ref é obrigatório")

    now = base.utc_now()
    payload = {
        "version": 1,
        "enabled": True,
        "action_id": action_id,
        "operation": "reboot",
        "executor_host": local_host,
        "target_host": target,
        "scope": f"host://{target}/reboot-once-remote",
        "owner_fingerprint": base.owner_fingerprint(),
        "authorized_at": base.utc_iso(now),
        "expires_at": base.utc_iso(now + timedelta(minutes=minutes)),
        "authorization_ref_sha256": hashlib.sha256(
            authorization_ref.encode("utf-8")
        ).hexdigest(),
        "consumed_at": None,
    }
    base.atomic_json(config_path, payload)
    append_audit(
        {
            "schema_version": "1.0.0",
            "event": "owner_remote_host_power_authorized",
            "action_id": action_id,
            "executor_host": local_host,
            "target_host": target,
            "operation": "reboot",
            "expires_at": payload["expires_at"],
            "authorization_ref_sha256": payload["authorization_ref_sha256"],
            "timestamp": base.utc_iso(),
        }
    )
    return {
        "ok": True,
        "action_id": action_id,
        "executor_host": local_host,
        "target_host": target,
        "operation": "reboot",
        "expires_at": payload["expires_at"],
    }


def load_authorization(
    *,
    action_id: str,
    config_path: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    base.validate_action_id(action_id)
    if not config_path.is_file():
        raise base.HostPowerError("autorização remota inexistente")
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise base.HostPowerError("autorização remota inválida") from exc

    local_host = socket.gethostname()
    target = validate_target_host(str(payload.get("target_host", "")))
    if payload.get("version") != 1 or payload.get("enabled") is not True:
        raise base.HostPowerError("autorização remota não está habilitada")
    if payload.get("action_id") != action_id:
        raise base.HostPowerError("action_id não corresponde à autorização remota")
    if payload.get("operation") != "reboot":
        raise base.HostPowerError("somente reboot remoto é permitido")
    if str(payload.get("executor_host", "")).casefold() != local_host.casefold():
        raise base.HostPowerError("autorização remota pertence a outro executor")
    if payload.get("scope") != f"host://{target}/reboot-once-remote":
        raise base.HostPowerError("escopo remoto inválido")
    if payload.get("owner_fingerprint") != base.owner_fingerprint():
        raise base.HostPowerError("autorização remota pertence a outro usuário/host")
    if payload.get("consumed_at"):
        raise base.HostPowerError("autorização remota já foi consumida")

    authorized_at = base.parse_utc(payload.get("authorized_at"))
    expires_at = base.parse_utc(payload.get("expires_at"))
    if expires_at - authorized_at > timedelta(
        minutes=MAX_WINDOW_MINUTES, seconds=5
    ):
        raise base.HostPowerError("janela de autorização remota excede o limite")
    reference = now or base.utc_now()
    if expires_at <= reference:
        raise base.HostPowerError("autorização remota expirada", base.EXIT_EXPIRED)
    return payload


def consume_authorization(
    *,
    config_path: Path,
    payload: dict[str, Any],
    correlation_id: str,
) -> None:
    consumed = dict(payload)
    consumed["consumed_at"] = base.utc_iso()
    consumed["correlation_id"] = correlation_id
    base.atomic_json(config_path, consumed)
    append_audit(
        {
            "schema_version": "1.0.0",
            "event": "owner_remote_host_power_consumed",
            "action_id": consumed["action_id"],
            "executor_host": consumed["executor_host"],
            "target_host": consumed["target_host"],
            "operation": "reboot",
            "correlation_id": correlation_id,
            "timestamp": consumed["consumed_at"],
        }
    )


def enable_remote_shutdown_privilege(kernel32, advapi32) -> None:
    token = base.wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(),
        base.TOKEN_ADJUST_PRIVILEGES | base.TOKEN_QUERY,
        ctypes.byref(token),
    ):
        raise base.HostPowerError(
            f"OpenProcessToken falhou com win32={base.get_windows_last_error()}",
            base.EXIT_COMMAND,
        )
    try:
        luid = base.LUID()
        if not advapi32.LookupPrivilegeValueW(
            None,
            "SeRemoteShutdownPrivilege",
            ctypes.byref(luid),
        ):
            raise base.HostPowerError(
                f"LookupPrivilegeValueW falhou com win32={base.get_windows_last_error()}",
                base.EXIT_COMMAND,
            )
        privileges = base.TOKEN_PRIVILEGES_ONE()
        privileges.PrivilegeCount = 1
        privileges.Privileges[0].Luid = luid
        privileges.Privileges[0].Attributes = base.SE_PRIVILEGE_ENABLED
        base.set_windows_last_error(0)
        if not advapi32.AdjustTokenPrivileges(
            token,
            False,
            ctypes.byref(privileges),
            0,
            None,
            None,
        ):
            raise base.HostPowerError(
                f"AdjustTokenPrivileges falhou com win32={base.get_windows_last_error()}",
                base.EXIT_COMMAND,
            )
        privilege_error = base.get_windows_last_error()
        if privilege_error == base.ERROR_NOT_ALL_ASSIGNED:
            raise base.HostPowerError(
                "SeRemoteShutdownPrivilege não está atribuído ao executor",
                base.EXIT_COMMAND,
            )
        if privilege_error != 0:
            raise base.HostPowerError(
                f"AdjustTokenPrivileges retornou win32={privilege_error}",
                base.EXIT_COMMAND,
            )
    finally:
        kernel32.CloseHandle(token)


def submit_remote_reboot(
    target_host: str,
    delay_seconds: int,
) -> subprocess.CompletedProcess[str]:
    target = validate_target_host(target_host)
    if delay_seconds < 5 or delay_seconds > 60:
        raise base.HostPowerError("delay de reboot deve estar entre 5 e 60 segundos")

    kernel32, advapi32 = base.windows_shutdown_api()
    enable_remote_shutdown_privilege(kernel32, advapi32)

    base.set_windows_last_error(0)
    accepted = advapi32.InitiateSystemShutdownExW(
        "\\\\" + target,
        "ReqSys governed one-time remote reboot validation",
        delay_seconds,
        False,
        True,
        base.planned_application_maintenance_reason(),
    )
    if not accepted:
        error = base.get_windows_last_error()
        return subprocess.CompletedProcess(
            ["InitiateSystemShutdownExW", target],
            error or 1,
            stdout="",
            stderr=f"win32={error}",
        )
    return subprocess.CompletedProcess(
        ["InitiateSystemShutdownExW", target],
        0,
        stdout="accepted",
        stderr="",
    )


def execute(
    *,
    action_id: str,
    confirm: str,
    config_path: Path,
    correlation_id: str,
    delay_seconds: int,
) -> dict[str, Any]:
    if confirm != EXECUTE_CONFIRM:
        raise base.HostPowerError("confirmação de execução remota inválida")
    payload = load_authorization(action_id=action_id, config_path=config_path)

    consume_authorization(
        config_path=config_path,
        payload=payload,
        correlation_id=correlation_id,
    )
    completed = submit_remote_reboot(payload["target_host"], delay_seconds)
    append_audit(
        {
            "schema_version": "1.0.0",
            "event": "owner_remote_host_power_submitted",
            "action_id": action_id,
            "executor_host": payload["executor_host"],
            "target_host": payload["target_host"],
            "operation": "reboot",
            "correlation_id": correlation_id,
            "returncode": completed.returncode,
            "stdout_sha256": hashlib.sha256(
                completed.stdout.encode("utf-8", errors="replace")
            ).hexdigest(),
            "stderr_sha256": hashlib.sha256(
                completed.stderr.encode("utf-8", errors="replace")
            ).hexdigest(),
            "timestamp": base.utc_iso(),
        }
    )
    if completed.returncode != 0:
        raise base.HostPowerError(
            f"reboot remoto governado falhou com exit={completed.returncode}",
            base.EXIT_COMMAND,
        )
    return {
        "ok": True,
        "action_id": action_id,
        "executor_host": payload["executor_host"],
        "target_host": payload["target_host"],
        "operation": "reboot",
        "correlation_id": correlation_id,
        "delay_seconds": delay_seconds,
        "consumed": True,
    }


def revoke(*, confirm: str, config_path: Path) -> dict[str, Any]:
    if confirm != REVOKE_CONFIRM:
        raise base.HostPowerError("confirmação de revogação remota inválida")
    if config_path.exists():
        config_path.unlink()
    append_audit(
        {
            "schema_version": "1.0.0",
            "event": "owner_remote_host_power_revoked",
            "timestamp": base.utc_iso(),
        }
    )
    return {"ok": True, "revoked": True}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Exceção governada e consumível para um único reboot remoto"
    )
    parser.add_argument("--config", type=Path, default=default_config_path())
    sub = parser.add_subparsers(dest="command", required=True)

    authorize_parser = sub.add_parser("authorize")
    authorize_parser.add_argument("--action-id", required=True)
    authorize_parser.add_argument("--executor-host", required=True)
    authorize_parser.add_argument("--target-host", required=True)
    authorize_parser.add_argument("--minutes", type=int, default=10)
    authorize_parser.add_argument("--authorization-ref", required=True)
    authorize_parser.add_argument("--confirm", required=True)

    execute_parser = sub.add_parser("execute")
    execute_parser.add_argument("--action-id", required=True)
    execute_parser.add_argument("--correlation-id", default=None)
    execute_parser.add_argument("--delay-seconds", type=int, default=5)
    execute_parser.add_argument("--confirm", required=True)

    revoke_parser = sub.add_parser("revoke")
    revoke_parser.add_argument("--confirm", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "authorize":
            result = authorize(
                action_id=args.action_id,
                executor_host=args.executor_host,
                target_host=args.target_host,
                minutes=args.minutes,
                confirm=args.confirm,
                config_path=args.config,
                authorization_ref=args.authorization_ref,
            )
        elif args.command == "execute":
            result = execute(
                action_id=args.action_id,
                confirm=args.confirm,
                config_path=args.config,
                correlation_id=args.correlation_id or str(uuid.uuid4()),
                delay_seconds=args.delay_seconds,
            )
        else:
            result = revoke(confirm=args.confirm, config_path=args.config)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except base.HostPowerError as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc), "exit_code": exc.exit_code},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())

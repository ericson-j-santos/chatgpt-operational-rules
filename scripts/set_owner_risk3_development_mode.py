#!/usr/bin/env python3
"""Ativa/desativa o modo temporário de desenvolvimento do Owner Risk 3 Gateway.

O arquivo local continua privado e vinculado ao fingerprint usuário+máquina.
Este helper nunca lê, imprime ou persiste segredo; altera apenas metadados de
política para local/DEV.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from owner_risk3_gateway import (  # noqa: E402
    DEV_MODE_MAX_DAYS,
    DEV_MODE_REASON,
    default_config_path,
    owner_fingerprint,
)

ENABLE_CONFIRMATION = "ENABLE-RISK3-DEV-MODE-STANDARD-GOLD"
DISABLE_CONFIRMATION = "DISABLE-RISK3-DEV-MODE"
DEFAULT_ALLOWED_ROOTS = [
    r"C:\dev\reqsys-v2-enterprise-real",
    r"C:\dev\wt-*",
    r"C:\dev\chatgpt-workers\*",
]


class ModeError(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _load_or_initialize(path: Path) -> dict:
    if path.name != "owner-risk3-exceptions.local.json" or not path.is_absolute():
        raise ModeError("caminho de configuração local inválido")
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ModeError("configuração local inválida") from exc
        if data.get("version") != 1:
            raise ModeError("versão da configuração local inválida")
        if data.get("owner_fingerprint") != owner_fingerprint():
            raise ModeError("configuração local pertence a outro usuário/máquina")
        if data.get("enabled") is not True:
            data["enabled"] = True
        if not isinstance(data.get("actions"), dict):
            data["actions"] = {}
        return data
    return {
        "version": 1,
        "enabled": True,
        "owner_fingerprint": owner_fingerprint(),
        "actions": {},
    }


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix="risk3-dev-mode-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
        if os.name != "nt":
            os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    finally:
        try:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        except OSError:
            pass


def enable(path: Path, days: int) -> dict:
    if days < 1 or days > DEV_MODE_MAX_DAYS:
        raise ModeError(f"days deve estar entre 1 e {DEV_MODE_MAX_DAYS}")
    data = _load_or_initialize(path)
    now = utc_now()
    expires = now + timedelta(days=days)
    data["development_mode"] = {
        "enabled": True,
        "environment": "dev",
        "reason": DEV_MODE_REASON,
        "activated_at": now.isoformat(),
        "expires_at": expires.isoformat(),
        "allowed_roots": DEFAULT_ALLOWED_ROOTS,
        "allowed_executables": ["python", "python3"],
        "reactivation_required_before_non_dev": True,
    }
    _atomic_write(path, data)
    return {
        "status": "enabled",
        "environment": "dev",
        "reason": DEV_MODE_REASON,
        "expires_at": expires.isoformat(),
        "reactivation_required_before_non_dev": True,
    }


def disable(path: Path) -> dict:
    data = _load_or_initialize(path)
    mode = data.get("development_mode")
    if not isinstance(mode, dict):
        mode = {}
    mode["enabled"] = False
    mode["disabled_at"] = utc_now().isoformat()
    mode["reactivation_required_before_non_dev"] = False
    data["development_mode"] = mode
    _atomic_write(path, data)
    return {"status": "disabled", "allowlist_required": True}


def status(path: Path) -> dict:
    data = _load_or_initialize(path)
    mode = data.get("development_mode")
    if not isinstance(mode, dict):
        return {"status": "disabled", "allowlist_required": True}
    return {
        "status": "enabled" if mode.get("enabled") is True else "disabled",
        "environment": mode.get("environment"),
        "reason": mode.get("reason"),
        "expires_at": mode.get("expires_at"),
        "reactivation_required_before_non_dev": bool(mode.get("reactivation_required_before_non_dev")),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Controla o modo temporário de desenvolvimento Risk 3")
    parser.add_argument("--config", type=Path, default=default_config_path())
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--enable", action="store_true")
    group.add_argument("--disable", action="store_true")
    group.add_argument("--status", action="store_true")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--confirm")
    return parser.parse_args()


def main() -> int:
    ns = parse_args()
    path = ns.config.expanduser().resolve()
    try:
        if ns.status:
            result = status(path)
        elif ns.enable:
            if ns.confirm != ENABLE_CONFIRMATION:
                raise ModeError(f"confirmação inválida; use {ENABLE_CONFIRMATION}")
            result = enable(path, ns.days)
        else:
            if ns.confirm != DISABLE_CONFIRMATION:
                raise ModeError(f"confirmação inválida; use {DISABLE_CONFIRMATION}")
            result = disable(path)
    except ModeError as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 20
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

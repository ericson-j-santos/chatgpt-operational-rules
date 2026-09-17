#!/usr/bin/env python3
"""Registra uma ação Risk 3 exata, temporária e auditável na configuração local."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import owner_risk3_gateway as risk3


def atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def register(config_path: Path, action_id: str, scope: str, environment: str, minutes: int, command: list[str]) -> dict:
    if minutes < 1 or minutes > 60:
        raise risk3.Risk3Error("validade deve estar entre 1 e 60 minutos")
    config = risk3.load_local_config(config_path)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    action = {
        "environment": environment,
        "scope": scope,
        "expires_at": expires_at.isoformat(),
        "command": command,
    }
    risk3.validate_action(action_id, scope, action)
    actions = config.setdefault("actions", {})
    if not isinstance(actions, dict):
        raise risk3.Risk3Error("bloco actions inválido")
    actions[action_id] = action
    atomic_write(config_path, config)
    return {"status": "registered", "action_id": action_id, "scope": scope, "expires_at": action["expires_at"]}


def main() -> int:
    parser = argparse.ArgumentParser(description="Cadastra ação exata no Owner Risk 3 Gateway")
    parser.add_argument("--config", type=Path, default=risk3.default_config_path())
    parser.add_argument("--action-id", required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--environment", choices=sorted(risk3.SAFE_ENVIRONMENTS), default="dev")
    parser.add_argument("--minutes", type=int, default=15)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    ns = parser.parse_args()
    command = list(ns.command)
    if command and command[0] == "--":
        command = command[1:]
    try:
        result = register(ns.config.expanduser().resolve(), ns.action_id, ns.scope, ns.environment, ns.minutes, command)
    except risk3.Risk3Error as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}))
        return exc.exit_code
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

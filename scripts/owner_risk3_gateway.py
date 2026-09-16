#!/usr/bin/env python3
"""Owner-only Risk 3 gateway.

This is a narrow exception path. The canonical command_gateway keeps Risk 3 denied.
Only exact, locally allowlisted actions can run here.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EXIT_POLICY = 20
EXIT_COMMAND = 22
EXIT_TIMEOUT = 24
EXIT_EXPIRED = 26

SAFE_ENVIRONMENTS = {"local", "dev"}
FORBIDDEN_EXECUTABLES = {
    "cmd", "powershell", "pwsh", "bash", "sh", "wsl", "ssh", "scp"
}
FORBIDDEN_ROLE_NAMES = {
    "owner", "contributor", "user access administrator",
    "role based access control administrator",
}
FORBIDDEN_DESTRUCTIVE_TOKENS = {
    "delete", "purge", "destroy", "drop", "prune", "reset", "format", "wipe"
}
SHELL_META = ("&&", "||", ";", "|", ">", "<", "`", "$(")
SECRET_OPTION_RE = re.compile(
    r"(?i)(?:^|[-_])(token|secret|password|passwd|api[-_]?key|client[-_]?secret)(?:$|[-_=])"
)
SECRET_VALUE_RE = re.compile(
    r"(?i)\b(token|secret|password|passwd|api[_-]?key|authorization)\b\s*[:=]\s*\S+"
)
PRODUCTION_MARKER_RE = re.compile(r"(?i)(^|[/:._-])(prod|production)([/:._-]|$)")


class Risk3Error(RuntimeError):
    def __init__(self, message: str, exit_code: int = EXIT_POLICY) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Risk3Error("expires_at inválido") from exc
    if parsed.tzinfo is None:
        raise Risk3Error("expires_at deve conter timezone")
    return parsed.astimezone(timezone.utc)


def owner_fingerprint() -> str:
    identity = f"{getpass.getuser()}@{socket.gethostname()}".encode("utf-8", errors="replace")
    return hashlib.sha256(identity).hexdigest()


def default_state_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "ReqSys" / "CommandGateway"
    return Path.home() / ".reqsys-command-gateway"


def default_config_path() -> Path:
    return default_state_dir() / "owner-risk3-exceptions.local.json"


def sha256_json(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def load_local_config(path: Path) -> dict[str, Any]:
    if not path.is_absolute():
        raise Risk3Error("configuração risco 3 deve usar caminho absoluto")
    if path.name != "owner-risk3-exceptions.local.json":
        raise Risk3Error("arquivo local de exceção possui nome inesperado")
    if not path.is_file():
        raise Risk3Error("configuração local de risco 3 inexistente")
    if os.name != "nt":
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            raise Risk3Error("configuração local deve ser privada (chmod 600)")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Risk3Error("configuração local de risco 3 inválida") from exc
    if data.get("version") != 1 or data.get("enabled") is not True:
        raise Risk3Error("exceção risco 3 local não está habilitada")
    expected = data.get("owner_fingerprint")
    if not isinstance(expected, str) or expected != owner_fingerprint():
        raise Risk3Error("exceção risco 3 não pertence ao usuário/máquina atual")
    return data


def normalize_executable(value: str) -> str:
    exe = Path(value).name.casefold()
    return exe[:-4] if exe.endswith(".exe") else exe


def validate_action(action_id: str, requested_scope: str, action: dict[str, Any]) -> list[str]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,127}", action_id):
        raise Risk3Error("action_id inválido")
    environment = str(action.get("environment", "")).casefold()
    if environment not in SAFE_ENVIRONMENTS:
        raise Risk3Error("risco 3 proprietário limitado a local/dev")
    configured_scope = action.get("scope")
    if not isinstance(configured_scope, str) or not configured_scope or configured_scope != requested_scope:
        raise Risk3Error("escopo não corresponde à allowlist local")
    if PRODUCTION_MARKER_RE.search(action_id) or PRODUCTION_MARKER_RE.search(requested_scope):
        raise Risk3Error("produção permanece bloqueada")
    expires_at = action.get("expires_at")
    if not isinstance(expires_at, str) or parse_utc(expires_at) <= datetime.now(timezone.utc):
        raise Risk3Error("exceção risco 3 expirada", EXIT_EXPIRED)

    command = action.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
        raise Risk3Error("comando allowlisted inválido")
    exe = normalize_executable(command[0])
    if exe in FORBIDDEN_EXECUTABLES:
        raise Risk3Error(f"executável proibido mesmo em risco 3: {exe}")
    if any(meta in arg for arg in command for meta in SHELL_META):
        raise Risk3Error("metacaractere/composição de shell proibido")
    lowered = [arg.casefold() for arg in command[1:]]
    if any(PRODUCTION_MARKER_RE.search(arg) for arg in command):
        raise Risk3Error("produção permanece bloqueada")
    if exe == "az" and len(lowered) >= 2 and lowered[0] == "keyvault" and lowered[1] == "secret":
        raise Risk3Error("leitura/escrita direta de segredos permanece bloqueada")
    if any(SECRET_VALUE_RE.search(arg) for arg in command):
        raise Risk3Error("valor potencialmente secreto embutido no comando")
    if any(SECRET_OPTION_RE.search(arg) for arg in command[1:]):
        raise Risk3Error("opção de segredo/credencial proibida em risco 3")

    if any(token in FORBIDDEN_DESTRUCTIVE_TOKENS for token in lowered):
        raise Risk3Error("operação destrutiva permanece bloqueada")
    if any(role in FORBIDDEN_ROLE_NAMES for role in lowered):
        raise Risk3Error("papel administrativo amplo permanece bloqueado")
    if "billing" in " ".join(lowered) or "microsoft.billing" in requested_scope.casefold():
        raise Risk3Error("billing permanece bloqueado")
    return command


def append_audit(state_dir: Path, payload: dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "risk3-audit.jsonl"
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def execute_action(
    *, config_path: Path, action_id: str, scope: str, cwd: Path,
    timeout: int, correlation_id: str,
) -> int:
    config = load_local_config(config_path)
    actions = config.get("actions")
    if not isinstance(actions, dict) or action_id not in actions or not isinstance(actions[action_id], dict):
        raise Risk3Error("action_id não está allowlisted localmente")
    command = validate_action(action_id, scope, actions[action_id])
    if timeout < 1 or timeout > 900:
        raise Risk3Error("timeout fora do limite 1..900s")
    if not cwd.is_dir():
        raise Risk3Error("cwd inexistente")

    started = utc_now()
    audit_base = {
        "schema_version": "1.0.0",
        "event": "owner_risk3_execution",
        "correlation_id": correlation_id,
        "action_id": action_id,
        "environment": actions[action_id].get("environment"),
        "owner_fingerprint_hash": hashlib.sha256(owner_fingerprint().encode("ascii")).hexdigest(),
        "scope_sha256": hashlib.sha256(scope.encode("utf-8")).hexdigest(),
        "command_sha256": sha256_json(command),
        "started_at": started,
    }
    try:
        completed = subprocess.run(
            command, cwd=str(cwd), shell=False, text=True,
            capture_output=True, timeout=timeout, check=False,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        append_audit(default_state_dir(), {**audit_base, "finished_at": utc_now(), "result": "timeout"})
        raise Risk3Error("execução risco 3 excedeu timeout", EXIT_TIMEOUT) from exc

    append_audit(default_state_dir(), {
        **audit_base,
        "finished_at": utc_now(),
        "result": "success" if completed.returncode == 0 else "command_failed",
        "returncode": completed.returncode,
        "stdout_sha256": hashlib.sha256(completed.stdout.encode("utf-8", errors="replace")).hexdigest(),
        "stderr_sha256": hashlib.sha256(completed.stderr.encode("utf-8", errors="replace")).hexdigest(),
    })
    if completed.returncode != 0:
        raise Risk3Error(f"comando allowlisted falhou com exit={completed.returncode}", EXIT_COMMAND)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Exceção local, restrita e auditável para ações Risk 3 do proprietário")
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--action-id")
    parser.add_argument("--scope")
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--correlation-id", default=None)
    parser.add_argument("--print-owner-fingerprint", action="store_true")
    return parser


def main() -> int:
    ns = build_parser().parse_args()
    if ns.print_owner_fingerprint:
        print(owner_fingerprint())
        return 0
    if not ns.action_id or not ns.scope:
        print(json.dumps({"status": "blocked", "reason": "--action-id e --scope são obrigatórios"}), file=sys.stderr)
        return EXIT_POLICY
    correlation_id = ns.correlation_id or str(uuid.uuid4())
    try:
        return execute_action(
            config_path=ns.config.expanduser(), action_id=ns.action_id,
            scope=ns.scope, cwd=ns.cwd.resolve(), timeout=ns.timeout,
            correlation_id=correlation_id,
        )
    except Risk3Error as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc), "correlation_id": correlation_id}), file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Owner-only, one-shot reboot gateway for a single local host authorization."""

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
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ACTION_ID = "host.reboot.desktop_primary"
AUTH_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,128}$")


def state_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "ReqSys" / "CommandGateway"
    return Path.home() / ".reqsys-command-gateway"


def default_config() -> Path:
    return state_dir() / "owner-risk3-reboot.local.json"


def default_audit() -> Path:
    return state_dir() / "risk3-reboot-audit.jsonl"


def fingerprint() -> str:
    raw = f"{getpass.getuser()}@{socket.gethostname()}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def authorization_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def fail(msg: str, code: int = 20) -> int:
    print(json.dumps({"status": "blocked", "reason": msg}), file=sys.stderr)
    return code


def audit(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp_name, 0o600)
        except OSError:
            pass
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def validate_config(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("version") != 1 or data.get("enabled") is not True:
        raise RuntimeError("exceção desabilitada")
    if data.get("action_id") != ACTION_ID:
        raise RuntimeError("ação divergente")
    if data.get("environment") != "local":
        raise RuntimeError("ambiente inválido")
    if data.get("owner_fingerprint") != fingerprint():
        raise RuntimeError("exceção não pertence ao usuário/máquina atual")

    host = str(data.get("host") or "").strip()
    if not host or socket.gethostname().casefold() != host.casefold():
        raise RuntimeError("host não autorizado")
    expected_scope = f"host://{host}/reboot"
    if str(data.get("scope") or "").casefold() != expected_scope.casefold():
        raise RuntimeError("escopo divergente")

    expires = datetime.fromisoformat(str(data["expires_at"]).replace("Z", "+00:00"))
    if expires.astimezone(timezone.utc) <= datetime.now(timezone.utc):
        raise RuntimeError("exceção expirada")

    authorization_id = str(data.get("authorization_id") or "")
    if not AUTH_ID_RE.fullmatch(authorization_id):
        raise RuntimeError("authorization_id ausente ou inválido")
    if data.get("consumed_at"):
        raise RuntimeError("autorização já consumida")

    script = Path(str(data.get("reboot_script") or ""))
    if not script.is_file():
        raise RuntimeError("script de reboot governado ausente")
    return data


def load_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError("configuração local ausente")
    return validate_config(json.loads(path.read_text(encoding="utf-8")))


def consume_authorization(
    config_path: Path,
    audit_path: Path,
    expected_authorization_id: str,
) -> dict[str, Any]:
    lock_path = config_path.with_name(config_path.name + ".lock")
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError("autorização em uso por outra execução") from exc

    try:
        os.write(lock_fd, f"{os.getpid()}\n".encode("ascii"))
        os.close(lock_fd)
        current = load_config(config_path)
        if current["authorization_id"] != expected_authorization_id:
            raise RuntimeError("authorization_id mudou durante a execução")

        current["enabled"] = False
        current["consumed_at"] = now()
        current["consumed_reason"] = "reboot_requested"
        atomic_write_json(config_path, current)

        persisted = json.loads(config_path.read_text(encoding="utf-8"))
        if persisted.get("enabled") is not False or not persisted.get("consumed_at"):
            raise RuntimeError("falha ao persistir consumo da autorização")
        audit(
            audit_path,
            {
                "event": "owner_risk3_reboot_authorization_consumed",
                "action_id": ACTION_ID,
                "authorization_sha256": authorization_hash(expected_authorization_id),
                "at": now(),
                "result": "consumed",
            },
        )
        return persisted
    finally:
        try:
            os.close(lock_fd)
        except OSError:
            pass
        lock_path.unlink(missing_ok=True)


def run_gateway(config_path: Path, audit_path: Path, *, check: bool) -> int:
    try:
        cfg = load_config(config_path)
        script = Path(cfg["reboot_script"])
        auth_hash = authorization_hash(cfg["authorization_id"])

        if check:
            completed = subprocess.run(
                [sys.executable, str(script), "--check"],
                shell=False,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError("pré-check do reboot falhou")
            audit(
                audit_path,
                {
                    "event": "owner_risk3_reboot_check",
                    "action_id": ACTION_ID,
                    "authorization_sha256": auth_hash,
                    "at": now(),
                    "result": "ok",
                },
            )
            print("OWNER_RISK3_REBOOT_CHECK_OK")
            return 0

        audit(
            audit_path,
            {
                "event": "owner_risk3_reboot_request",
                "action_id": ACTION_ID,
                "authorization_sha256": auth_hash,
                "at": now(),
                "result": "authorized",
            },
        )
        consume_authorization(config_path, audit_path, cfg["authorization_id"])

        completed = subprocess.run(
            [sys.executable, str(script)],
            shell=False,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            audit(
                audit_path,
                {
                    "event": "owner_risk3_reboot_request",
                    "action_id": ACTION_ID,
                    "authorization_sha256": auth_hash,
                    "at": now(),
                    "result": "failed_after_consume",
                    "returncode": completed.returncode,
                },
            )
            raise RuntimeError(f"reboot governado falhou exit={completed.returncode}")

        print("OWNER_RISK3_REBOOT_ACCEPTED_ONE_SHOT")
        return 0
    except Exception as exc:
        audit(
            audit_path,
            {
                "event": "owner_risk3_reboot_request",
                "action_id": ACTION_ID,
                "at": now(),
                "result": "blocked",
                "reason": str(exc),
            },
        )
        return fail(str(exc))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--config", type=Path, default=default_config())
    parser.add_argument("--audit", type=Path, default=default_audit())
    args = parser.parse_args()
    return run_gateway(args.config.resolve(), args.audit.resolve(), check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())

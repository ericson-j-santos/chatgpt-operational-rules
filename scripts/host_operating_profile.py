#!/usr/bin/env python3
"""Gerencia o perfil operacional local de um host da centralizadora."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VALID_PROFILES = {"NORMAL", "ESTUDO"}


def default_profile_path() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "ReqSys" / "TodoGlobal24x7" / "host-profile.json"
    return Path.home() / ".config" / "todo-global-24x7" / "host-profile.json"


def _normalize_profile(value: object) -> str:
    profile = str(value or "").strip().upper()
    if profile not in VALID_PROFILES:
        raise ValueError("profile deve ser NORMAL ou ESTUDO")
    return profile


def load_profile(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": "1",
            "host": None,
            "profile": "NORMAL",
            "accepts_new_development": True,
            "source": "default",
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("profile file deve conter objeto JSON")
    profile = _normalize_profile(payload.get("profile"))
    result = dict(payload)
    result["profile"] = profile
    result["accepts_new_development"] = profile == "NORMAL"
    result["source"] = "file"
    return result


def write_profile(
    path: Path,
    *,
    host: str,
    profile: str,
    correlation_id: str,
) -> dict[str, Any]:
    host = host.strip()
    correlation_id = correlation_id.strip()
    if not host:
        raise ValueError("host é obrigatório")
    if not 8 <= len(correlation_id) <= 128:
        raise ValueError("correlation_id deve ter 8..128 caracteres")

    normalized = _normalize_profile(profile)
    payload = {
        "schema_version": "1",
        "host": host,
        "profile": normalized,
        "accepts_new_development": normalized == "NORMAL",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "correlation_id": correlation_id,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Perfil NORMAL/ESTUDO do host")
    parser.add_argument("--path", type=Path, default=default_profile_path())
    sub = parser.add_subparsers(dest="command", required=True)

    set_cmd = sub.add_parser("set")
    set_cmd.add_argument("--host", required=True)
    set_cmd.add_argument("--profile", required=True, choices=sorted(VALID_PROFILES))
    set_cmd.add_argument("--correlation-id", required=True)

    sub.add_parser("show")
    args = parser.parse_args()

    try:
        result = (
            write_profile(
                args.path,
                host=args.host,
                profile=args.profile,
                correlation_id=args.correlation_id,
            )
            if args.command == "set"
            else load_profile(args.path)
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2

    print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

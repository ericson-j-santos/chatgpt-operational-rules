#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from scripts.todo_gateway_pc24x7 import ensure_runtime_env, runtime_dir

PENDING_AUTH_FILE = "webhook-auth.pending"


def upsert_env(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    found: set[str] = set()
    output: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line else ""
        if key in updates:
            output.append(f"{key}={updates[key]}")
            found.add(key)
        else:
            output.append(line)
    for key, value in updates.items():
        if key not in found:
            output.append(f"{key}={value}")
    temp = path.with_name(path.name + ".tmp")
    temp.write_text("\n".join(output) + "\n", encoding="utf-8", newline="\n")
    os.replace(temp, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    pending = runtime_dir() / PENDING_AUTH_FILE
    if not pending.is_file():
        raise SystemExit("arquivo de autenticação pendente ausente")
    secret = pending.read_text(encoding="utf-8").strip()
    if len(secret) < 32:
        raise SystemExit("material de autenticação GitLab inválido")
    env_path = ensure_runtime_env()
    upsert_env(env_path, {
        "GITLAB_WEBHOOK_TOKEN": secret,
        "GITLAB_WEBHOOK_PROJECT": args.project.strip(),
    })
    fingerprint = hashlib.sha256(secret.encode()).hexdigest()[:12]
    pending.unlink(missing_ok=True)
    print(json.dumps({"result": "WEBHOOK_ENV_CONFIGURED", "project": args.project.strip(), "auth_sha256_12": fingerprint}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

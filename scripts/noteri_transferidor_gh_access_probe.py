#!/usr/bin/env python3
"""Consulta fixa de acesso ao commit privado TRANSF-008 pelo GitHub CLI instalado.

Somente leitura, sem criação de credenciais, sem shell/GUI e sem argumentos de destino.
Executar via Session Launcher + Command Gateway, no checkout Git versionado.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
from typing import Any, Callable

from noteri_transferidor_gcm_oauth import profile_ok, no_secret_environment

EXPECTED_CLI = Path(r"C:\Program Files\GitHub CLI\gh.exe")
API_RESOURCE = "repos/ericson-j-santos/transferidor-documental/git/ref/heads/main"
EXPECTED_SHA = "fd78bba91f15ead41990972154addd77284374c2"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def receipt(state: str) -> dict[str, Any]:
    return {
        "schema_version": "1", "task_id": "TRANSF-008", "host": "Noteri",
        "source_reference": "main", "source_sha": EXPECTED_SHA,
        "result": state, "authenticated_access_verified": state == "PRIVATE_ACCESS_VERIFIED",
        "secret_value_exposed": False, "state_changed": False,
    }


def probe(*, os_name: str, hostname: str, localappdata: str | None,
          expected_cli: Path, observed_cli: str | None,
          environment: dict[str, str], runner: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    if not profile_ok(os_name=os_name, hostname=hostname, localappdata=localappdata):
        return receipt("BLOCKED_HOST_OR_PROFILE")
    if not observed_cli:
        return receipt("GH_CLI_UNAVAILABLE")
    try:
        if (not expected_cli.is_file() or expected_cli.is_symlink()
            or not (1024 < expected_cli.stat().st_size < 150 * 1024 * 1024)
            or Path(observed_cli).resolve(strict=True) != expected_cli.resolve(strict=True)):
            return receipt("GH_CLI_ORIGIN_UNVERIFIED")
    except (OSError, ValueError):
        return receipt("GH_CLI_ORIGIN_UNVERIFIED")
    env = no_secret_environment(environment)
    env.pop("GH_CONFIG_DIR", None)
    env.pop("GH_DEBUG", None)
    env.pop("GH_ENTERPRISE_TOKEN", None)
    env.pop("GITHUB_ENTERPRISE_TOKEN", None)
    env["GH_HOST"] = "github.com"
    env["GH_PROMPT_DISABLED"] = "1"
    env["GH_PAGER"] = ""
    env["GCM_INTERACTIVE"] = "never"
    argv = [str(expected_cli), "api", "--hostname", "github.com", "--method", "GET",
            API_RESOURCE, "--jq", ".object.sha"]
    try:
        p = runner(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                   stderr=subprocess.DEVNULL, timeout=25, check=False,
                   shell=False, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return receipt("GH_AUTH_PROBE_FAILED")
    # A referência de SHA é a única informação retornável: jamais emitir stderr,
    # ambiente, identidade de conta ou resposta bruta de uma API.
    if p.returncode != 0:
        return receipt("GH_AUTH_OR_PERMISSION_MISSING")
    try:
        value = p.stdout.decode("ascii", errors="strict").strip() if isinstance(p.stdout, bytes) else str(p.stdout).strip()
    except (UnicodeError, TypeError):
        return receipt("GH_REF_RESPONSE_INVALID")
    if len(value) != 40 or not SHA_RE.fullmatch(value):
        return receipt("GH_REF_RESPONSE_INVALID")
    if value != EXPECTED_SHA:
        return receipt("BLOCKED_HEAD_MISMATCH")
    return receipt("PRIVATE_ACCESS_VERIFIED")


def main() -> int:
    result = probe(
        os_name=os.name, hostname=socket.gethostname(),
        localappdata=os.environ.get("LOCALAPPDATA"),
        expected_cli=EXPECTED_CLI, observed_cli=shutil.which("gh"),
        environment=os.environ,
    )
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result["result"] == "PRIVATE_ACCESS_VERIFIED" else 28


if __name__ == "__main__":
    raise SystemExit(main())

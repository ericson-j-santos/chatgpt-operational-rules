#!/usr/bin/env python3
"""Apply and independently verify minimal GitHub branch protection.

Uses an already-provisioned Git credential from the OS credential manager.
Never prints or persists the credential.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
API_VERSION = "2022-11-28"


class ProtectionError(RuntimeError):
    pass


def credential() -> str:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    proc = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        raise ProtectionError("credencial GitHub indisponível no cofre Git")
    values: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    token = values.get("password", "")
    if not token:
        raise ProtectionError("credencial GitHub indisponível no cofre Git")
    return token


def request_json(method: str, path: str, token: str, payload: dict | None = None) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "chatgpt-governed-branch-protection/1.0",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {}
        return exc.code, body


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--required-check", required=True)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()

    if args.confirm != "PROTECT-CENTRALHAB-MAIN":
        print(json.dumps({"result": "BLOCKED", "error": "confirmação inválida"}, ensure_ascii=False))
        return 2

    token = credential()
    path = f"/repos/{args.owner}/{args.repo}/branches/{args.branch}/protection"
    payload = {
        "required_status_checks": {
            "strict": True,
            "contexts": [args.required_check],
        },
        "enforce_admins": True,
        "required_pull_request_reviews": None,
        "restrictions": None,
        "required_linear_history": False,
        "allow_force_pushes": False,
        "allow_deletions": False,
        "block_creations": False,
        "required_conversation_resolution": True,
        "lock_branch": False,
        "allow_fork_syncing": False,
    }

    status, body = request_json("PUT", path, token, payload)
    if status != 200:
        message = str(body.get("message", "erro não especificado"))[:200]
        print(json.dumps({"result": "BLOCKED", "http_status": status, "error": message}, ensure_ascii=False))
        return 1

    read_status, verified = request_json("GET", path, token)
    if read_status != 200:
        print(json.dumps({"result": "BLOCKED", "error": f"leitura pós-alteração falhou: HTTP {read_status}"}, ensure_ascii=False))
        return 1

    checks = [
        item.get("context")
        for item in verified.get("required_status_checks", {}).get("checks", [])
        if isinstance(item, dict)
    ]
    if not checks:
        checks = verified.get("required_status_checks", {}).get("contexts", []) or []
    admins = bool(verified.get("enforce_admins", {}).get("enabled"))
    conversations = bool(verified.get("required_conversation_resolution", {}).get("enabled"))
    if args.required_check not in checks or not admins:
        print(json.dumps({
            "result": "BLOCKED",
            "error": "proteção aplicada parcialmente",
            "required_checks": checks,
            "enforce_admins": admins,
            "required_conversation_resolution": conversations,
        }, ensure_ascii=False, sort_keys=True))
        return 1

    print(json.dumps({
        "result": "PROTECTION_OK",
        "repository": f"{args.owner}/{args.repo}",
        "branch": args.branch,
        "required_checks": checks,
        "enforce_admins": admins,
        "required_conversation_resolution": conversations,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

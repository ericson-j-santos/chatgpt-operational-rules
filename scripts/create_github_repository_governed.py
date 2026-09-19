#!/usr/bin/env python3
"""Cria de forma idempotente um repositório GitHub privado para um projeto autorizado."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
API_VERSION = "2022-11-28"
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


class RepoCreateError(RuntimeError):
    pass


def token_from_env() -> str:
    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if not token:
        raise RepoCreateError("GITHUB_TOKEN/GH_TOKEN não provisionado")
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
            "User-Agent": "chatgpt-governed-repo-bootstrap/1.0",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed = {}
        return exc.code, parsed


def verify_repo(owner: str, name: str, token: str) -> dict | None:
    status, data = request_json("GET", f"/repos/{owner}/{name}", token)
    if status == 404:
        return None
    if status != 200:
        raise RepoCreateError(f"falha ao verificar repositório: HTTP {status}")
    if data.get("full_name") != f"{owner}/{name}":
        raise RepoCreateError("repositório retornado não corresponde ao alvo")
    if data.get("private") is not True:
        raise RepoCreateError("repositório existente não é privado; operação recusada")
    return data


def create_private_repo(owner: str, name: str, description: str, token: str) -> dict:
    existing = verify_repo(owner, name, token)
    if existing is not None:
        return {"result": "ALREADY_EXISTS_PRIVATE", "full_name": existing["full_name"], "default_branch": existing.get("default_branch")}

    status, data = request_json(
        "POST",
        "/user/repos",
        token,
        {
            "name": name,
            "description": description,
            "private": True,
            "auto_init": True,
            "has_issues": True,
            "has_projects": False,
            "has_wiki": False,
            "delete_branch_on_merge": True,
        },
    )
    if status != 201:
        message = str(data.get("message", "erro não especificado"))[:200]
        raise RepoCreateError(f"criação recusada: HTTP {status}: {message}")

    verified = verify_repo(owner, name, token)
    if verified is None:
        raise RepoCreateError("repositório não encontrado na leitura independente pós-criação")
    return {
        "result": "CREATED_PRIVATE",
        "full_name": verified["full_name"],
        "default_branch": verified.get("default_branch"),
        "private": verified.get("private"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--description", required=True)
    args = parser.parse_args()

    if not NAME_RE.fullmatch(args.owner) or not NAME_RE.fullmatch(args.name):
        print(json.dumps({"result": "BLOCKED", "error": "owner/name inválido"}, ensure_ascii=False))
        return 2

    try:
        result = create_private_repo(args.owner, args.name, args.description, token_from_env())
    except RepoCreateError as exc:
        print(json.dumps({"result": "BLOCKED", "error": str(exc)}, ensure_ascii=False))
        return 1

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

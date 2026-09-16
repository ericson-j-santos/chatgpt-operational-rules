#!/usr/bin/env python3
"""Materializa um checkout Git limpo em destino novo e permitido.

Rota de recuperação pré-sessão para quando o clone existente não consegue
passar no preflight. Nunca altera, reseta ou remove o clone antigo.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

EXIT_MATERIALIZE = 29
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
REPOSITORIES = {
    "ericson-j-santos/reqsys-v2-enterprise-real":
        "https://github.com/ericson-j-santos/reqsys-v2-enterprise-real.git",
}


class MaterializeError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_sha(value: str) -> str:
    if not SHA_RE.fullmatch(value):
        raise MaterializeError("commit deve ser SHA completo de 40 caracteres")
    return value.lower()


def validate_self(expected: str) -> None:
    expected = expected.strip().lower()
    if len(expected) != 64 or sha256_file(Path(__file__).resolve()) != expected:
        raise MaterializeError("SHA-256 do materializador divergente")


def resolve_git() -> str:
    git = shutil.which("git")
    if not git:
        raise MaterializeError("git não encontrado no host")
    return git


def ensure_destination(destination: Path, work_root: Path) -> tuple[Path, Path]:
    root = work_root.resolve()
    target = destination.resolve()
    try:
        common = Path(os.path.commonpath([str(root), str(target)]))
    except ValueError as exc:
        raise MaterializeError("destino fora do work root") from exc
    if common != root or target == root:
        raise MaterializeError("destino deve estar abaixo do work root")
    if target.exists():
        raise MaterializeError("destino já existe; materialização exige diretório novo")
    return root, target


def run_git(git: str, args: list[str], *, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        [git, *args], cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180, check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip().replace("\r", " ").replace("\n", " ")
        raise MaterializeError(f"git falhou rc={completed.returncode}: {stderr[:400]}")
    return completed.stdout.strip()


def materialize(repository: str, commit: str, destination: Path, work_root: Path) -> dict:
    if repository not in REPOSITORIES:
        raise MaterializeError("repositório não permitido para materialização")
    expected = validate_sha(commit)
    _, target = ensure_destination(destination, work_root)
    git = resolve_git()
    created = False
    try:
        target.mkdir(parents=True, exist_ok=False)
        created = True
        run_git(git, ["init", "--quiet"], cwd=target)
        run_git(git, ["remote", "add", "origin", REPOSITORIES[repository]], cwd=target)
        run_git(git, ["fetch", "--quiet", "--depth=1", "origin", expected], cwd=target)
        run_git(git, ["checkout", "--quiet", "--detach", expected], cwd=target)
        head = run_git(git, ["rev-parse", "HEAD"], cwd=target).lower()
        origin = run_git(git, ["remote", "get-url", "origin"], cwd=target)
        status = run_git(git, ["status", "--porcelain=v1"], cwd=target)
        if head != expected:
            raise MaterializeError(f"HEAD materializado diverge do esperado: {head}")
        if origin != REPOSITORIES[repository]:
            raise MaterializeError("origin materializado diverge do repositório permitido")
        if status:
            raise MaterializeError("checkout materializado não está limpo")
        return {
            "result": "CLEAN_REPO_MATERIALIZED",
            "timestamp": utc_now(),
            "repository": repository,
            "origin": origin,
            "commit": head,
            "destination": str(target),
            "clean": True,
        }
    except Exception:
        if created and target.exists():
            shutil.rmtree(target, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Materializador governado de checkout limpo")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, default=Path(r"C:\dev\chatgpt-workers"))
    parser.add_argument("--expected-self-sha256", required=True)
    ns = parser.parse_args()
    try:
        validate_self(ns.expected_self_sha256)
        payload = materialize(ns.repository, ns.commit, ns.destination, ns.work_root)
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({
            "result": "CLEAN_REPO_MATERIALIZE_BLOCKED",
            "timestamp": utc_now(),
            "gateway_exit_code": EXIT_MATERIALIZE,
            "error": str(exc),
        }, ensure_ascii=True, sort_keys=True), file=sys.stderr)
        return EXIT_MATERIALIZE


if __name__ == "__main__":
    raise SystemExit(main())

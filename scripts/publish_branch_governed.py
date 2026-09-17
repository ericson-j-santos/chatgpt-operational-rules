#!/usr/bin/env python3
"""Publica uma branch Git de forma restrita, sem force-push ou branch protegida."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
BRANCH_RE = re.compile(r"^(?![./])(?!.*\.\.)(?!.*[~^:?*\[\\\s])(?!.*(?:^|/)\.)(?!.*\.lock(?:/|$))[A-Za-z0-9._/-]+(?<![/.])$")
PROTECTED = {"main", "master", "develop", "development", "stg", "stage", "staging", "prod", "production"}


def run(repo: Path, *args: str) -> str:
    cp = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, timeout=60, check=False)
    if cp.returncode:
        raise RuntimeError((cp.stderr or cp.stdout or "git command failed").strip())
    return cp.stdout.strip()


def validate_branch(branch: str) -> str:
    value = branch.strip()
    if not BRANCH_RE.fullmatch(value) or value.lower() in PROTECTED:
        raise ValueError("branch de destino inválida ou protegida")
    return value


def publish(repo: Path, branch: str, expected_head: str) -> dict:
    repo = repo.resolve()
    branch = validate_branch(branch)
    expected = expected_head.lower()
    if not SHA_RE.fullmatch(expected):
        raise ValueError("expected-head deve ser SHA completo")
    head = run(repo, "rev-parse", "HEAD").lower()
    if head != expected:
        raise RuntimeError(f"HEAD divergente: atual={head} esperado={expected}")
    if run(repo, "status", "--porcelain"):
        raise RuntimeError("worktree deve estar limpo")
    current = run(repo, "branch", "--show-current")
    if current != branch:
        raise RuntimeError(f"branch local divergente: atual={current} esperado={branch}")
    remote_before = run(repo, "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    if remote_before:
        remote_sha = remote_before.split()[0].lower()
        if remote_sha == head:
            return {"result": "ALREADY_PUBLISHED", "branch": branch, "head": head, "remote_sha": remote_sha}
        raise RuntimeError("branch remota já existe em SHA diferente; atualização recusada")
    run(repo, "push", "origin", f"HEAD:refs/heads/{branch}")
    remote_after = run(repo, "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    if not remote_after or remote_after.split()[0].lower() != head:
        raise RuntimeError("publicação não foi confirmada por leitura independente")
    return {"result": "PUBLISHED", "branch": branch, "head": head, "remote_sha": head}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--repo", type=Path, required=True)
    p.add_argument("--branch", required=True)
    p.add_argument("--expected-head", required=True)
    ns = p.parse_args()
    try:
        print(json.dumps(publish(ns.repo, ns.branch, ns.expected_head), ensure_ascii=False))
        return 0
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"result": "BLOCKED", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 31


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Apply and verify minimal GitHub branch protection for Central de Pesquisas."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

OWNER = "ericson-j-santos"
REPO = "central-pesquisas-habitacionais"
BRANCH = "main"
REQUIRED_CONTEXT = "integrity-self-test"
CONFIRM = "PROTECT-CENTRALHAB-MAIN"


class ProtectionError(RuntimeError):
    pass


def credential() -> str:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProtectionError("GitHub credential unavailable") from exc
    if proc.returncode != 0:
        raise ProtectionError("GitHub credential unavailable")
    values: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    token = values.get("password", "")
    if not token:
        raise ProtectionError("GitHub credential unavailable")
    return token


def api(method: str, path: str, token: str, payload: dict | None = None) -> tuple[int, dict]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.github.com" + path,
        data=body,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "chatgpt-governed-branch-protection/1.0",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {}
        return exc.code, parsed


def verify(data: dict) -> None:
    checks = data.get("required_status_checks") or {}
    contexts = checks.get("contexts") or []
    pr_reviews = data.get("required_pull_request_reviews")
    admins = data.get("enforce_admins") or {}
    if REQUIRED_CONTEXT not in contexts:
        raise ProtectionError("required status check not enforced")
    if checks.get("strict") is not True:
        raise ProtectionError("strict status checks not enforced")
    if pr_reviews is None:
        raise ProtectionError("pull request requirement not enforced")
    if admins.get("enabled") is not True:
        raise ProtectionError("admin enforcement not enabled")


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] != "--confirm" or sys.argv[2] != CONFIRM:
        print(json.dumps({"result": "BLOCKED", "reason": "explicit confirmation mismatch"}))
        return 2

    token = credential()
    repo_path = f"/repos/{OWNER}/{REPO}"
    status, repo = api("GET", repo_path, token)
    if status != 200 or repo.get("full_name") != f"{OWNER}/{REPO}":
        print(json.dumps({"result": "BLOCKED", "reason": f"repository verification failed HTTP {status}"}))
        return 1

    protection_path = f"{repo_path}/branches/{BRANCH}/protection"
    payload = {
        "required_status_checks": {
            "strict": True,
            "contexts": [REQUIRED_CONTEXT],
        },
        "enforce_admins": True,
        "required_pull_request_reviews": {
            "dismiss_stale_reviews": False,
            "require_code_owner_reviews": False,
            "required_approving_review_count": 0,
            "require_last_push_approval": False,
        },
        "restrictions": None,
        "required_linear_history": False,
        "allow_force_pushes": False,
        "allow_deletions": False,
        "block_creations": False,
        "required_conversation_resolution": False,
        "lock_branch": False,
        "allow_fork_syncing": False,
    }

    status, response = api("PUT", protection_path, token, payload)
    if status != 200:
        message = str(response.get("message", "unspecified GitHub error"))[:240]
        print(json.dumps({"result": "BLOCKED", "reason": f"GitHub HTTP {status}: {message}"}))
        return 1

    status, observed = api("GET", protection_path, token)
    if status != 200:
        print(json.dumps({"result": "BLOCKED", "reason": f"post-write verification HTTP {status}"}))
        return 1
    try:
        verify(observed)
    except ProtectionError as exc:
        print(json.dumps({"result": "BLOCKED", "reason": str(exc)}))
        return 1

    print(json.dumps({
        "result": "PROTECTION_OK",
        "repository": f"{OWNER}/{REPO}",
        "branch": BRANCH,
        "required_status_check": REQUIRED_CONTEXT,
        "strict": True,
        "require_pull_request": True,
        "enforce_admins": True,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

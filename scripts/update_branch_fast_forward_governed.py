#!/usr/bin/env python3
"""Reviewed, lease-checked fast-forward maintenance for exact branch updates."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
EXPECTED_REPOSITORY = "ericson-j-santos/desktop-pc24x7-runtime"
EXPECTED_BRANCH = "fix/rp23ogs-host-label-isolation-draft-gating-20261015"
EXPECTED_REMOTE_SHA = "3f88cd5c35c1ab85c3dc13a30ad0bc645a1ff6b5"
EXPECTED_HEAD_SHA = "22b435ea287971e3e1ff0126d7cc3f096eae5747"
EXPECTED_REPO_ROOT = (
    r"C:\dev\chatgpt-workers\wt-chat-rp23ogs-diag-desktop-rp23-20261015-110652-19cc"
)
WORKER_REPOSITORY = "ericson-j-santos/engineering-worker-pool"
WORKER_BRANCH = "codex/windows-native-no-docker-20261022"
WORKER_REMOTE_SHA = "8740def7e832916452b13c52a1f4bec2f5712df7"
WORKER_HEAD_SHA = "cd150974a094dcda240987d8a2283c709a714d36"
WORKER_REPO_ROOT = (
    r"C:\dev\chatgpt-workers\wt-chat-engineering-worker-pool-base-20261022-a1"
)
NOTERI_REPOSITORY = "ericson-j-santos/noteri-runtime"
NOTERI_BRANCH = "feat/noteri-sql-app-eventlog-readonly-gs-20261025-r1"
NOTERI_REMOTE_SHA = "0c83b07a8d9e69758ddd2c25eb40833539ef1889"
NOTERI_HEAD_SHA = "d8f058d66f46f8fcccf52a178d082fcb1d9b5222"
NOTERI_REPO_ROOT = (
    r"C:\dev\chatgpt-workers\wt-chat-noteri-eventlog-reconcile-gs-20261025-r2"
)
RULES_REPOSITORY = "ericson-j-santos/chatgpt-operational-rules"
RULES_BRANCH = "feat/noteri-sql-eventlog-dispatch-allowlist-20261025"
RULES_REMOTE_SHA = "546b2b2ded661576b9553f0fec62864ae798fdb6"
RULES_HEAD_SHA = "2eb124bf457b9e121141eaca7dd124865374eaeb"
RULES_REPO_ROOT = (
    r"C:\dev\chatgpt-workers\wt-chat-noteri-eventlog-rules-reconcile-gs-20261025-r2"
)
LENOVO_PR112_REPOSITORY = "ericson-j-santos/desktop-pc24x7-runtime"
LENOVO_PR112_BRANCH = "fix/lenovo-native-task-readback-20261027"
LENOVO_PR112_REMOTE_SHA = "04d7788513618bfd3cf39c283747f9e3f0d88397"
LENOVO_PR112_HEAD_SHA = "0f2337012a7926d2327cfbdd8931147354d1db68"
LENOVO_PR112_REPO_ROOT = r"C:\dev\chatgpt-workers\wt-chat-lenovo-task-readback-20261027"


def reviewed_operations() -> tuple[dict[str, str], ...]:
    return (
        {
            "repository": EXPECTED_REPOSITORY,
            "branch": EXPECTED_BRANCH,
            "expected_remote": EXPECTED_REMOTE_SHA,
            "expected_head": EXPECTED_HEAD_SHA,
            "repo_root": EXPECTED_REPO_ROOT,
        },
        {
            "repository": WORKER_REPOSITORY,
            "branch": WORKER_BRANCH,
            "expected_remote": WORKER_REMOTE_SHA,
            "expected_head": WORKER_HEAD_SHA,
            "repo_root": WORKER_REPO_ROOT,
        },
        {
            "repository": NOTERI_REPOSITORY,
            "branch": NOTERI_BRANCH,
            "expected_remote": NOTERI_REMOTE_SHA,
            "expected_head": NOTERI_HEAD_SHA,
            "repo_root": NOTERI_REPO_ROOT,
        },
        {
            "repository": RULES_REPOSITORY,
            "branch": RULES_BRANCH,
            "expected_remote": RULES_REMOTE_SHA,
            "expected_head": RULES_HEAD_SHA,
            "repo_root": RULES_REPO_ROOT,
        },
        {
            "repository": LENOVO_PR112_REPOSITORY,
            "branch": LENOVO_PR112_BRANCH,
            "expected_remote": LENOVO_PR112_REMOTE_SHA,
            "expected_head": LENOVO_PR112_HEAD_SHA,
            "repo_root": LENOVO_PR112_REPO_ROOT,
        },
    )


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "git command failed").strip()
        raise RuntimeError(detail)
    return result


def remote_sha(repo: Path, remote: str, branch: str) -> str:
    result = git(repo, "ls-remote", "--heads", remote, f"refs/heads/{branch}")
    return result.stdout.split()[0].lower() if result.stdout.split() else ""


def canonical_remote(value: str) -> str:
    value = value.strip().removesuffix(".git")
    if value.startswith("https://github.com/"):
        return value.removeprefix("https://github.com/").lower()
    if value.startswith("git@github.com:"):
        return value.removeprefix("git@github.com:").lower()
    return value.lower()


def validate(repo: Path, remote: str, branch: str, expected_remote: str, expected_head: str) -> dict[str, str]:
    resolved = repo.resolve()
    if not SHA_RE.fullmatch(expected_remote) or not SHA_RE.fullmatch(expected_head):
        raise RuntimeError("expected SHA must be complete")
    matches = [
        operation for operation in reviewed_operations()
        if remote == "origin"
        and branch == operation["branch"]
        and expected_remote == operation["expected_remote"]
        and expected_head == operation["expected_head"]
    ]
    if len(matches) != 1:
        raise RuntimeError("arguments are outside the reviewed branch update scope")
    operation = matches[0]
    if str(resolved).casefold() != operation["repo_root"].casefold():
        raise RuntimeError("repository path is outside the reviewed branch update scope")

    repository = canonical_remote(git(repo, "remote", "get-url", remote).stdout)
    if repository != operation["repository"]:
        raise RuntimeError("origin is not the reviewed repository")
    if git(repo, "branch", "--show-current").stdout.strip() != branch:
        raise RuntimeError("local branch does not match the reviewed branch")
    if git(repo, "rev-parse", "HEAD").stdout.strip().lower() != expected_head:
        raise RuntimeError("local HEAD does not match expected-head")
    if git(repo, "status", "--porcelain").stdout.strip():
        raise RuntimeError("worktree must be clean")

    observed_remote = remote_sha(repo, remote, branch)
    if observed_remote != expected_remote:
        raise RuntimeError("remote changed before maintenance push")
    ancestor = git(repo, "merge-base", "--is-ancestor", expected_remote, expected_head, check=False)
    if ancestor.returncode != 0:
        raise RuntimeError("target is not a fast-forward descendant of expected remote")

    return {
        "repository": operation["repository"],
        "branch": branch,
        "expected_remote": expected_remote,
        "expected_head": expected_head,
        "observed_remote": observed_remote,
        "fast_forward": "true",
        "force_push": "false",
        "remote_compare_atomic": "false",
    }


def update(repo: Path, remote: str, branch: str, expected_remote: str, expected_head: str) -> dict[str, str]:
    evidence = validate(repo, remote, branch, expected_remote, expected_head)
    second_remote = remote_sha(repo, remote, branch)
    if second_remote != expected_remote:
        raise RuntimeError("remote changed during pre-push checks")

    git(repo, "push", remote, f"HEAD:refs/heads/{branch}")
    after = remote_sha(repo, remote, branch)
    if after != expected_head:
        raise RuntimeError("post-push remote readback did not reach expected-head")
    evidence["post_remote"] = after
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", required=True)
    parser.add_argument("--expected-remote", required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--correlation-id", required=True)
    args = parser.parse_args()
    try:
        evidence = update(args.repo, args.remote, args.branch, args.expected_remote.lower(), args.expected_head.lower())
        evidence.update({"result": "UPDATED", "correlation_id": args.correlation_id})
        print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(
            json.dumps(
                {
                    "result": "BLOCKED",
                    "correlation_id": args.correlation_id,
                    "error": str(exc),
                    "remote_compare_atomic": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 31


if __name__ == "__main__":
    raise SystemExit(main())

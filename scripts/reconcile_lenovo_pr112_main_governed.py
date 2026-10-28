#!/usr/bin/env python3
"""One-shot, tree-equivalent merge of reviewed main into Lenovo PR #112."""
from __future__ import annotations

import argparse
import json
import re
import socket
import subprocess
import sys
from pathlib import Path


REPO = Path(r"C:\dev\chatgpt-workers\wt-chat-lenovo-task-readback-20261027")
BRANCH = "fix/lenovo-native-task-readback-20261027"
OLD_HEAD = "04d7788513618bfd3cf39c283747f9e3f0d88397"
MAIN_SHA = "bfdef6008faeab3e2e6634bec079c00e45eebf6a"
BASE_SHA = "7733f72b00c85f5f78885b38f5ee89434bcb5dc9"
REPOSITORY = "ericson-j-santos/desktop-pc24x7-runtime"
CONFIRM = "RECONCILE-LENOVO-PR112-MAIN"
SHA = re.compile(r"^[0-9a-f]{40}$")
CORRELATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")


class ReconcileBlocked(RuntimeError):
    pass


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    if check and result.returncode != 0:
        raise ReconcileBlocked("git_precondition_or_update_failed")
    return result


def output(*args: str) -> str:
    return git(*args).stdout.strip()


def remote_sha(branch: str) -> str:
    words = output("ls-remote", "--heads", "origin", f"refs/heads/{branch}").split()
    return words[0].lower() if words else ""


def tree(commit: str) -> str:
    value = output("show", "-s", "--format=%T", commit).lower()
    if not SHA.fullmatch(value):
        raise ReconcileBlocked("tree_identity_invalid")
    return value


def parents(commit: str) -> list[str]:
    return output("show", "-s", "--format=%P", commit).lower().split()


def reconcile(correlation_id: str) -> dict[str, object]:
    if not CORRELATION.fullmatch(correlation_id):
        raise ReconcileBlocked("correlation_id_invalid")
    if socket.gethostname().casefold() != "desktop-rp23ogs":
        raise ReconcileBlocked("host_mismatch")
    if str(REPO.resolve()).casefold() != str(REPO).casefold():
        raise ReconcileBlocked("repo_path_redirected")
    if output("remote", "get-url", "origin").lower().removesuffix(".git") != (
        f"https://github.com/{REPOSITORY}"
    ):
        raise ReconcileBlocked("origin_mismatch")
    if output("branch", "--show-current") != BRANCH:
        raise ReconcileBlocked("branch_mismatch")
    if output("status", "--porcelain"):
        raise ReconcileBlocked("worktree_dirty")

    current = output("rev-parse", "HEAD").lower()
    if current != OLD_HEAD:
        if parents(current) == [OLD_HEAD, MAIN_SHA] and tree(current) == tree(OLD_HEAD):
            if remote_sha(BRANCH) not in {OLD_HEAD, current}:
                raise ReconcileBlocked("remote_branch_changed")
            return {
                "result": "ALREADY_RECONCILED",
                "head": current,
                "parents": [OLD_HEAD, MAIN_SHA],
                "tree_unchanged": True,
                "correlation_id": correlation_id,
            }
        raise ReconcileBlocked("head_mismatch")

    if remote_sha(BRANCH) != OLD_HEAD or remote_sha("main") != MAIN_SHA:
        raise ReconcileBlocked("remote_ref_changed")
    if output("rev-parse", "origin/main").lower() != MAIN_SHA:
        raise ReconcileBlocked("local_main_ref_changed")
    if output("merge-base", OLD_HEAD, MAIN_SHA).lower() != BASE_SHA:
        raise ReconcileBlocked("merge_base_changed")
    old_parents = parents(OLD_HEAD)
    if len(old_parents) != 1 or tree(old_parents[0]) != tree(MAIN_SHA):
        raise ReconcileBlocked("main_tree_not_equivalent_to_parent")
    old_tree = tree(OLD_HEAD)
    if remote_sha(BRANCH) != OLD_HEAD or remote_sha("main") != MAIN_SHA:
        raise ReconcileBlocked("remote_ref_changed_during_preflight")

    created = output(
        "commit-tree", old_tree, "-p", OLD_HEAD, "-p", MAIN_SHA,
        "-m", "reconcile: include tree-equivalent main #111 in Lenovo PR #112",
    ).lower()
    if not SHA.fullmatch(created) or parents(created) != [OLD_HEAD, MAIN_SHA]:
        raise ReconcileBlocked("merge_commit_invalid")
    if tree(created) != old_tree:
        raise ReconcileBlocked("merge_tree_changed")
    git("update-ref", f"refs/heads/{BRANCH}", created, OLD_HEAD)
    if output("rev-parse", "HEAD").lower() != created or output("status", "--porcelain"):
        raise ReconcileBlocked("local_readback_failed")
    return {
        "result": "RECONCILED_LOCALLY",
        "head": created,
        "parents": [OLD_HEAD, MAIN_SHA],
        "tree_unchanged": True,
        "remote_compare_atomic": False,
        "correlation_id": correlation_id,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--correlation-id", required=True)
    args = parser.parse_args()
    if args.confirm != CONFIRM:
        print(json.dumps({"result": "BLOCKED", "reason": "confirmation_invalid"}))
        return 31
    try:
        print(json.dumps(reconcile(args.correlation_id), sort_keys=True))
        return 0
    except (OSError, ReconcileBlocked, subprocess.SubprocessError) as exc:
        reason = str(exc)
        if not isinstance(exc, ReconcileBlocked) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", reason):
            reason = "reconciliation_failed"
        print(json.dumps({"result": "BLOCKED", "reason": reason}, sort_keys=True), file=sys.stderr)
        return 31


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Rules #219: apply exactly the locally proposed, AST-constrained test.

No arbitrary path, model-authored command, shell, stage, commit or publish.
The installed Gateway must separately verify the command's exact digest.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from rules219_model_propose import ROOT, SOURCE, MODEL, require, git, state_dir, identity, candidate


def main() -> int:
    task, base, digest = identity()
    state = state_dir()
    proposal_file = state / "proposal.json"
    applied_file = state / "applied.json"
    require(proposal_file.is_file() and not proposal_file.is_symlink()
            and proposal_file.stat().st_size <= 32768
            and not applied_file.exists() and not applied_file.is_symlink(),
            "RULES219_PROPOSAL_MISSING_OR_ALREADY_APPLIED")
    require(git("status", "--porcelain", "--untracked-files=all") == b"",
            "RULES219_APPLY_WORKTREE_DIRTY")
    try:
        proposal = json.loads(proposal_file.read_bytes())
    except (OSError, ValueError, UnicodeError):
        raise SystemExit("RULES219_PROPOSAL_INVALID") from None
    expected = {
        "task_id", "source_sha", "source_path", "model", "digest",
        "origin", "process_verified", "model_calls", "before_sha256",
        "after_sha256", "model_reply_sha256", "raw_reply", "method",
    }
    require(type(proposal) is dict and set(proposal) == expected
            and proposal["task_id"] == task and proposal["source_sha"] == base
            and proposal["source_path"] == SOURCE and proposal["model"] == MODEL
            and proposal["digest"] == digest
            and proposal["origin"] == "http://127.0.0.1:11434"
            and proposal["process_verified"] is True
            and type(proposal["model_calls"]) is int
            and proposal["model_calls"] == 1
            and type(proposal["raw_reply"]) is str
            and hashlib.sha256(proposal["raw_reply"].encode()).hexdigest()
            == proposal["model_reply_sha256"],
            "RULES219_PROPOSAL_PROVENANCE_MISMATCH")
    try:
        reply = json.loads(proposal["raw_reply"])
    except ValueError:
        raise SystemExit("RULES219_PROPOSAL_REPLY_INVALID") from None
    require(type(reply) is dict and reply == {"method": proposal["method"]},
            "RULES219_PROPOSAL_REPLY_MISMATCH")
    target = ROOT / SOURCE
    require(target.is_file() and not target.is_symlink()
            and target.stat().st_nlink == 1 and target.stat().st_size < 100000,
            "RULES219_SOURCE_UNTRUSTED")
    before = target.read_bytes()
    require(hashlib.sha256(before).hexdigest() == proposal["before_sha256"],
            "RULES219_SOURCE_CHANGED")
    amended = candidate(before, proposal["method"])
    require(hashlib.sha256(amended).hexdigest() == proposal["after_sha256"],
            "RULES219_PROPOSED_HASH_MISMATCH")
    require(git("rev-parse", "HEAD").decode().strip() == base,
            "RULES219_BASE_DRIFT")
    # One reviewed target and no other path. A post-write crash is ambiguous:
    # do NOT retry automatically; external fenced controller must reconcile.
    target.write_bytes(amended)
    require(target.read_bytes() == amended
            and git("diff", "--name-only", "--no-renames", "-z") == (SOURCE + "\0").encode()
            and git("diff", "--cached", "--name-only", "-z") == b""
            and git("ls-files", "--others", "--exclude-standard", "-z") == b"",
            "RULES219_APPLY_SCOPE_DRIFT")
    receipt = {
        "task_id": task, "source_sha": base, "source_path": SOURCE,
        "before_sha256": proposal["before_sha256"],
        "after_sha256": proposal["after_sha256"],
        "model_reply_sha256": proposal["model_reply_sha256"],
        "result": "RULES219_MODEL_APPLIED_ONCE",
    }
    with applied_file.open("xb") as output:
        output.write(json.dumps(receipt, sort_keys=True).encode())
        output.flush()
        os.fsync(output.fileno())
    print("RULES219_MODEL_APPLIED_ONCE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

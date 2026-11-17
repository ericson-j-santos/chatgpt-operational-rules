#!/usr/bin/env python3
"""Rules #219: fixed read-only independent Validator for one source+manifest commit.

A separate Worker Pool Validator must own its own checkout, lease, Gateway and
CAS readback. This test receipt alone is NOT READY_FOR_PR or host attestation.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from rules219_model_propose import ROOT, SOURCE, END, require, git, state_dir, candidate

HEX40 = re.compile(r"[0-9a-f]{40}\Z")


def fixed_test(*args: str) -> str:
    result = subprocess.run(
        [sys.executable, "-B", *args], cwd=ROOT, capture_output=True,
        timeout=90, check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    require(result.returncode == 0 and len(result.stderr) <= 131072
            and len(result.stdout) <= 131072,
            "RULES219_VALIDATOR_TEST_FAILED")
    return (result.stdout + result.stderr).decode("utf-8", "replace")


def main() -> int:
    require(len(sys.argv) == 1, "RULES219_VALIDATOR_ARGUMENTS_FORBIDDEN")
    task = os.environ.get("RULES219_TASK_ID", "")
    base = os.environ.get("RULES219_APPROVED_SHA", "")
    produced = os.environ.get("RULES219_PRODUCED_SHA", "")
    require(re.fullmatch(r"cwp-[0-9a-f]{24}", task) is not None
            and HEX40.fullmatch(base) is not None
            and HEX40.fullmatch(produced) is not None
            and base != produced,
            "RULES219_VALIDATOR_CONTEXT_MISSING")
    require(Path(git("rev-parse", "--show-toplevel").decode().strip()).resolve() == ROOT
            and git("remote") == b""
            and git("rev-parse", "HEAD").decode().strip() == produced
            and git("status", "--porcelain", "--untracked-files=all") == b"",
            "RULES219_VALIDATOR_CHECKOUT_UNTRUSTED")
    parents = git("rev-list", "--parents", "-n", "1", "HEAD").decode().split()
    require(parents == [produced, base],
            "RULES219_VALIDATOR_COMMIT_PARENT_DRIFT")
    names = [p.decode() for p in git(
        "diff", "--no-renames", "--name-only", "-z", base, produced
    ).split(b"\0") if p]
    require(set(names) == {"MANIFEST.json", SOURCE} and len(names) == 2,
            "RULES219_VALIDATOR_DIFF_SCOPE_DRIFT")
    old = git("show", base + ":" + SOURCE)
    new = git("show", produced + ":" + SOURCE)
    require(new == (ROOT / SOURCE).read_bytes()
            and old.count(END.encode()) == 1
            and new.count(END.encode()) == 1,
            "RULES219_VALIDATOR_SOURCE_DRIFT")
    old_start, old_end = old.split(END.encode())
    new_start, new_end = new.split(END.encode())
    require(old_end == new_end and new_start.startswith(old_start + b"\n"),
            "RULES219_VALIDATOR_NONMODEL_CHANGE")
    try:
        method = new_start[len(old_start) + 1:].decode("utf-8")
    except UnicodeError:
        raise SystemExit("RULES219_VALIDATOR_GENERATED_TEST_INVALID") from None
    require(candidate(old, method) == new,
            "RULES219_VALIDATOR_GENERATED_TEST_UNSAFE")
    manifest_file = ROOT / "MANIFEST.json"
    manifest_bytes = manifest_file.read_bytes()
    require(git("show", "HEAD:MANIFEST.json") == manifest_bytes,
            "RULES219_VALIDATOR_MANIFEST_INDEX_DRIFT")
    try:
        entry = next(x for x in json.loads(manifest_bytes)["files"]
                     if x["path"] == SOURCE)
    except (ValueError, KeyError, TypeError, StopIteration):
        raise SystemExit("RULES219_VALIDATOR_MANIFEST_INVALID") from None
    require(entry.get("sha256") == hashlib.sha256(new).hexdigest()
            and entry.get("size") == len(new),
            "RULES219_VALIDATOR_MANIFEST_HASH_DRIFT")
    # Both checks are real, on THIS independent checkout. No network, model,
    # installation, Git writes or arbitrary tool provided by generated code.
    validation = fixed_test("scripts/validate_rules.py", "--self-test")
    require("VALIDATION_OK" in validation, "RULES219_VALIDATOR_RULES_NOT_VERIFIED")
    output = fixed_test("-m", "unittest", "-v",
                        "tests/test_generate_manifest_builder.py")
    match = re.search(r"Ran (\d+) tests? in ", output)
    require(match is not None and int(match.group(1)) >= 12
            and "\nOK" in output and "skipped=" not in output,
            "RULES219_VALIDATOR_UNITS_UNPROVED")
    for test in (
        "test_pinned_wrapper_rejects_caller_arguments_before_mutation",
        "test_source_and_manifest_staged_in_one_index",
        "test_untracked_file_blocks",
    ):
        require(re.search(re.escape(test) + r".*\.\.\. ok", output) is not None,
                "RULES219_VALIDATOR_NEGATIVE_OR_REPLAY_MISSING")
    state = state_dir()
    record = state / "validator.json"
    require(not record.exists() and not record.is_symlink(),
            "RULES219_VALIDATOR_ALREADY_RECORDED")
    report = {
        "result": "INDEPENDENT_TESTS_PASSED", "task_id": task,
        "source_sha": base, "produced_sha": produced,
        "validator_role": "validator", "tests_passed": int(match.group(1)),
        "tests_failed": 0, "negative_control_passed": True,
        "idempotency_passed": True,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "source_sha256": hashlib.sha256(new).hexdigest(),
    }
    with record.open("xb") as output_file:
        output_file.write(json.dumps(report, sort_keys=True).encode())
        output_file.flush()
        os.fsync(output_file.fileno())
    require(git("status", "--porcelain", "--untracked-files=all") == b"",
            "RULES219_VALIDATOR_CHANGED_CHECKOUT")
    print("RULES219_VALIDATION_RECEIPT_WRITTEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

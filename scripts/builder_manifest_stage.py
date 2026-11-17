#!/usr/bin/env python3
"""Fixed, digest-pinned Builder manifest staging entry point.

No caller-supplied arguments, shell, model input, commit, push or merge.
The installed Gateway and Worker Pool must separately approve this script's
exact SHA, repository worktree, Risk2 session and command profile.
"""
from __future__ import annotations

import hashlib
import runpy
import sys
from pathlib import Path

GENERATOR_SHA256 = "001e052388d74ba6b54fea84e2585312555aae71056984e36198ac2921185355"


def main() -> int:
    if len(sys.argv) != 1:
        raise SystemExit("BUILDER_MANIFEST_ARGUMENTS_FORBIDDEN")
    generator = Path(__file__).resolve().with_name("generate_manifest.py")
    if not generator.is_file() or generator.is_symlink():
        raise SystemExit("BUILDER_MANIFEST_SOURCE_UNTRUSTED")
    if hashlib.sha256(generator.read_bytes()).hexdigest() != GENERATOR_SHA256:
        raise SystemExit("BUILDER_MANIFEST_SOURCE_HASH_MISMATCH")
    # Execute only the fixed, hash-checked source with its one admissible mode.
    sys.argv = [str(generator), "--stage-for-builder"]
    runpy.run_path(str(generator), run_name="__main__")
    raise SystemExit("BUILDER_MANIFEST_UNEXPECTED_RETURN")


if __name__ == "__main__":
    raise SystemExit(main())

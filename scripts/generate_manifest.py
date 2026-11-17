#!/usr/bin/env python3
"""Regenera MANIFEST.json a partir das entradas existentes e paths obrigatórios."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True

# Windows embeddable Python can run with pythonXY._pth isolation, which omits
# the script directory from sys.path. Bootstrap the sibling validator explicitly.
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from validate_rules import REQUIRED_PATHS

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "MANIFEST.json"


def _git(*args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args], capture_output=True, check=True
    ).stdout


def _paths_from_nul(payload: bytes) -> set[str]:
    return {part.decode("utf-8") for part in payload.split(b"\0") if part}


def _prepare_builder_stage() -> set[str]:
    """Refuse to hash bytes that differ from the Git index to be committed."""
    if Path(_git("rev-parse", "--show-toplevel").decode().strip()).resolve() != ROOT:
        raise SystemExit("BUILDER_MANIFEST_REPOSITORY_MISMATCH")
    if _paths_from_nul(_git("diff", "--name-only", "-z")):
        raise SystemExit("BUILDER_MANIFEST_UNSTAGED_MODIFICATIONS")
    if _paths_from_nul(_git("ls-files", "--others", "--exclude-standard", "-z")):
        raise SystemExit("BUILDER_MANIFEST_UNTRACKED_FILES")
    staged = _paths_from_nul(_git("diff", "--cached", "--name-only", "-z"))
    if not staged - {"MANIFEST.json"}:
        raise SystemExit("BUILDER_MANIFEST_NO_SOURCE_STAGED")
    if "MANIFEST.json" not in _paths_from_nul(_git("ls-files", "-z")):
        raise SystemExit("BUILDER_MANIFEST_NOT_TRACKED")
    return staged


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--stage-for-builder", action="store_true",
        help="Regenera, valida e adiciona somente MANIFEST.json ao índice Git do Builder.",
    )
    args = parser.parse_args()
    staged_before = _prepare_builder_stage() if args.stage_for_builder else None
    current = json.loads(MANIFEST.read_text(encoding="utf-8"))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(r"^Versão:\s*(\S+)\s*$", readme, re.MULTILINE)
    if not match:
        raise SystemExit("README sem versão")
    listed = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True)
    paths = {item.decode("utf-8") for item in listed.stdout.split(b"\0") if item}
    paths.discard("MANIFEST.json")
    paths.update(REQUIRED_PATHS)
    entries = []
    for rel in sorted(paths):
        target = ROOT / rel
        if not target.is_file() or (args.stage_for_builder and target.is_symlink()):
            raise SystemExit(f"arquivo ausente ou inseguro: {rel}")
        payload = target.read_bytes()
        entries.append({"path": rel, "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)})
    data = {
        "version": match.group(1),
        "generated_at": (
            current.get("generated_at")
            if args.stage_for_builder and current.get("version") == match.group(1)
            and current.get("files") == entries
            and isinstance(current.get("generated_at"), str)
            else datetime.now(timezone.utc).isoformat()
        ),
        "files": entries,
    }
    output = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if not args.stage_for_builder or MANIFEST.read_text(encoding="utf-8") != output:
        MANIFEST.write_text(output, encoding="utf-8", newline="\n")
    if args.stage_for_builder:
        # A local check is mandatory but is not the independent Validator gate.
        check = subprocess.run(
            [sys.executable, "-B", str(SCRIPT_DIR / "validate_rules.py"), "--self-test"],
            cwd=ROOT, capture_output=True, check=False,
        )
        if check.returncode != 0:
            raise SystemExit("BUILDER_MANIFEST_VALIDATION_FAILED")
        _git("add", "--", "MANIFEST.json")
        if _git("diff", "--cached", "--check"):
            raise SystemExit("BUILDER_MANIFEST_INDEX_FORMAT_INVALID")
        if _git("show", ":MANIFEST.json") != MANIFEST.read_bytes():
            raise SystemExit("BUILDER_MANIFEST_INDEX_READBACK_MISMATCH")
        if _paths_from_nul(_git("diff", "--cached", "--name-only", "-z")) != (
            staged_before | {"MANIFEST.json"}
        ):
            raise SystemExit("BUILDER_MANIFEST_STAGED_SCOPE_CHANGED")
        if _paths_from_nul(_git("diff", "--name-only", "-z")):
            raise SystemExit("BUILDER_MANIFEST_UNSTAGED_AFTER_STAGE")
        # The real independent Validator still must run on the final commit.
        print(f"BUILDER_MANIFEST_STAGED files={len(entries)} source_staged={len(staged_before - {'MANIFEST.json'})}")
    print(f"MANIFEST_GENERATED version={data['version']} files={len(entries)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

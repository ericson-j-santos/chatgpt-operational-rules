#!/usr/bin/env python3
"""Autocorreção restrita de MANIFEST.json para PRs do próprio repositório."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

sys.dont_write_bytecode = True
from validate_rules import REQUIRED_PATHS

MANIFEST_NAME = "MANIFEST.json"


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout.strip()


def current_head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD")


def verify_expected_head(root: Path, expected_head: str) -> None:
    actual = current_head(root)
    if actual.lower() != expected_head.lower():
        raise RuntimeError(f"head_changed actual={actual} expected={expected_head}")


def verify_clean(root: Path) -> None:
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise RuntimeError("target_not_clean")


def _tracked_paths(root: Path) -> set[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return {item.decode("utf-8") for item in result.stdout.split(b"\0") if item}


def build_manifest(root: Path, required_paths: Iterable[str] = REQUIRED_PATHS) -> dict:
    readme = (root / "README.md").read_text(encoding="utf-8")
    match = re.search(r"^Versão:\s*(\S+)\s*$", readme, re.MULTILINE)
    if not match:
        raise RuntimeError("readme_without_version")

    paths = _tracked_paths(root)
    paths.discard(MANIFEST_NAME)
    paths.update(required_paths)

    entries = []
    for rel in sorted(paths):
        target = root / rel
        if not target.is_file():
            raise RuntimeError(f"required_or_tracked_file_missing:{rel}")
        payload = target.read_bytes()
        entries.append(
            {
                "path": rel,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
        )
    return {"version": match.group(1), "files": entries}


def regenerate_if_needed(root: Path, required_paths: Iterable[str] = REQUIRED_PATHS) -> tuple[bool, dict]:
    manifest_path = root / MANIFEST_NAME
    current = json.loads(manifest_path.read_text(encoding="utf-8"))
    desired = build_manifest(root, required_paths)
    if current.get("version") == desired["version"] and current.get("files") == desired["files"]:
        return False, current

    updated = {
        "version": desired["version"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": desired["files"],
    }
    manifest_path.write_text(
        json.dumps(updated, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return True, updated


def changed_paths(root: Path) -> list[str]:
    raw = _git(root, "diff", "--name-only")
    return [line for line in raw.splitlines() if line]


def main() -> int:
    parser = argparse.ArgumentParser(description="Self-heal restrito do manifesto")
    parser.add_argument("--target-root", type=Path, required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--report", type=Path)
    ns = parser.parse_args()

    root = ns.target_root.resolve()
    report: dict[str, object] = {
        "status": "blocked",
        "target_root": str(root),
        "expected_head": ns.expected_head,
    }
    try:
        verify_expected_head(root, ns.expected_head)
        verify_clean(root)
        changed, manifest = regenerate_if_needed(root)
        paths = changed_paths(root)
        if changed and paths != [MANIFEST_NAME]:
            raise RuntimeError(f"unsafe_changed_paths:{','.join(paths)}")
        if not changed and paths:
            raise RuntimeError(f"unexpected_dirty_after_no_change:{','.join(paths)}")
        report.update(
            {
                "status": "changed" if changed else "no_change",
                "head": current_head(root),
                "changed_paths": paths,
                "manifest_version": manifest.get("version"),
                "manifest_files": len(manifest.get("files", [])),
            }
        )
        code = 0
    except (OSError, ValueError, subprocess.CalledProcessError, RuntimeError, json.JSONDecodeError) as exc:
        report["reason"] = str(exc)
        code = 4

    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True)
    print(rendered)
    if ns.report:
        ns.report.parent.mkdir(parents=True, exist_ok=True)
        ns.report.write_text(rendered + "\n", encoding="utf-8", newline="\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

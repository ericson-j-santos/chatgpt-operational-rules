#!/usr/bin/env python3
"""Regenera MANIFEST.json a partir das entradas existentes e paths obrigatórios."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from validate_rules import REQUIRED_PATHS

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "MANIFEST.json"


def main() -> int:
    current = json.loads(MANIFEST.read_text(encoding="utf-8"))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(r"^Versão:\s*(\S+)\s*$", readme, re.MULTILINE)
    if not match:
        raise SystemExit("README sem versão")
    paths = {item["path"] for item in current.get("files", []) if isinstance(item, dict) and item.get("path")}
    paths.update(REQUIRED_PATHS)
    entries = []
    for rel in sorted(paths):
        target = ROOT / rel
        if not target.is_file():
            raise SystemExit(f"arquivo ausente: {rel}")
        payload = target.read_bytes()
        entries.append({"path": rel, "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)})
    data = {
        "version": match.group(1),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": entries,
    }
    MANIFEST.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"MANIFEST_GENERATED version={data['version']} files={len(entries)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

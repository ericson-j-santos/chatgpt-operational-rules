#!/usr/bin/env python3
"""Instala atomicamente o gateway one-shot de reboot no host local."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path


def default_target() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "ReqSys" / "CommandGateway" / "bin" / "owner_reboot_gateway.py"
    return Path.home() / ".reqsys-command-gateway" / "bin" / "owner_reboot_gateway.py"


def atomic_install(source: Path, target: Path) -> dict[str, object]:
    payload = source.read_bytes()
    expected = hashlib.sha256(payload).hexdigest()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)

    actual_payload = target.read_bytes()
    actual = hashlib.sha256(actual_payload).hexdigest()
    ready = actual == expected and actual_payload == payload
    return {
        "source": str(source),
        "target": str(target),
        "sha256": actual,
        "size": len(actual_payload),
        "ready": ready,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=Path, default=default_target())
    args = parser.parse_args()
    source = Path(__file__).with_name("owner_reboot_gateway.py")
    result = atomic_install(source, args.target.expanduser().resolve())
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

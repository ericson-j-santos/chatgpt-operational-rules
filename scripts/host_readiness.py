#!/usr/bin/env python3
"""Validate a host against the canonical non-secret Runtime Platform contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "config" / "host-readiness.json"


class ReadinessError(RuntimeError):
    """Raised when the readiness contract or evidence is invalid."""


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReadinessError(f"cannot_read_json:{path}:{exc}") from exc
    if not isinstance(data, dict):
        raise ReadinessError(f"json_root_must_be_object:{path}")
    return data


def evaluate(contract: dict[str, Any], profile: str, evidence: dict[str, Any] | None) -> dict[str, Any]:
    hosts = contract.get("hosts")
    if not isinstance(hosts, dict) or profile not in hosts:
        raise ReadinessError(f"unknown_profile:{profile}")
    host = hosts[profile]
    required = host.get("required_capabilities")
    if not isinstance(required, list) or not required or not all(isinstance(x, str) and x for x in required):
        raise ReadinessError(f"invalid_required_capabilities:{profile}")

    result: dict[str, Any] = {
        "schema_version": 1,
        "profile": profile,
        "runtime_repository": host.get("runtime_repository"),
        "required_capabilities": required,
        "missing_capabilities": list(required),
        "ready": False,
    }
    if evidence is None:
        result["status"] = "needs_evidence"
        return result

    if evidence.get("profile") != profile:
        raise ReadinessError("evidence_profile_mismatch")
    capabilities = evidence.get("capabilities")
    if not isinstance(capabilities, dict):
        raise ReadinessError("evidence_capabilities_must_be_object")

    missing = [name for name in required if capabilities.get(name) is not True]
    result["missing_capabilities"] = missing
    result["ready"] = not missing
    result["status"] = "ready" if not missing else "blocked"
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()

    try:
        contract = load_json(args.contract)
        evidence = load_json(args.evidence) if args.evidence else None
        result = evaluate(contract, args.profile, evidence)
    except ReadinessError as exc:
        print(json.dumps({"ready": False, "status": "invalid", "error": str(exc)}, sort_keys=True))
        return 2

    print(json.dumps(result, sort_keys=True))
    return 0 if result["ready"] or result["status"] == "needs_evidence" else 1


if __name__ == "__main__":
    raise SystemExit(main())

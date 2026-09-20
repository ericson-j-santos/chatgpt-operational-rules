#!/usr/bin/env python3
"""Classifica o resultado funcional de operações Remote Desktop Commander."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

FAIL_MARKERS = (
    "NO_CALLBACK",
    "SESSION_LAUNCH_BLOCKED",
    "StopIteration",
    "transport_guard revoke=true",
    "Channel subscription timed out",
    "Channel error:",
    "Channel closed",
    "Failed to update transport capability:",
)
FAIL_RESULTS = {
    "blocked",
    "failed",
    "error",
    "self_test_failed",
    "no_callback",
}
PASS_RESULTS = {
    "callback_ok",
    "self_test_passed",
    "fixed",
    "already_fixed",
    "already_applied",
    "ready_claim_arbitration_applied",
    "task_resilience_applied",
    "task_resilience_headless_applied",
}
SEMANTIC_BOOL_KEYS = {
    "callback_received",
    "transport_proven",
    "ready",
    "submitted",
    "opened_run",
    "enter",
    "selected",
    "copied",
    "pasted",
    "connected",
}
EMPTY_IS_FAILURE_KEYS = {"matched", "controls", "callbacks"}
EXIT_CODE_KEYS = {
    "exit_code",
    "command_exit_code",
    "gateway_exit_code",
    "returncode",
}


def _walk(value: Any, path: str = ""):
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            yield child, str(key), item
            yield from _walk(item, child)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            child = f"{path}[{index}]"
            yield child, str(index), item
            yield from _walk(item, child)


def _resolve(payload: Any, dotted: str) -> Any:
    current = payload
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(dotted)
        current = current[part]
    return current


def _is_truthy_semantic(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().casefold() in {
            "true",
            "ok",
            "ready",
            "connected",
            "received",
            "proven",
            "success",
        }
    if isinstance(value, (list, dict)):
        return bool(value)
    return False


def evaluate(payload: Any, required: tuple[str, ...] = ()) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("payload deve ser objeto JSON")

    reasons: list[str] = []
    positive: list[str] = []
    technical_codes: list[tuple[str, int]] = []

    for path, key, value in _walk(payload):
        key_fold = key.casefold()

        if key_fold in EXIT_CODE_KEYS and isinstance(value, int):
            technical_codes.append((path, value))
            if value != 0:
                reasons.append(f"technical_exit_nonzero:{path}={value}")

        if key_fold == "error" and value not in (None, "", False):
            reasons.append(f"functional_error:{path}")

        if key_fold == "result" and isinstance(value, str):
            normalized = value.strip().casefold()
            if normalized in FAIL_RESULTS:
                reasons.append(f"failure_result:{value}")
            elif normalized in PASS_RESULTS:
                positive.append(f"result:{value}")

        if key_fold in SEMANTIC_BOOL_KEYS:
            if value is False:
                reasons.append(f"semantic_false:{path}")
            elif value is True:
                positive.append(f"semantic_true:{path}")

        if key_fold in EMPTY_IS_FAILURE_KEYS and isinstance(value, (list, dict)):
            if not value:
                reasons.append(f"semantic_empty:{path}")
            else:
                positive.append(f"semantic_nonempty:{path}")

        if isinstance(value, str):
            for marker in FAIL_MARKERS:
                if marker.casefold() in value.casefold():
                    reasons.append(f"failure_marker:{marker}")
            text = value.casefold()
            for marker in (
                "result=callback_ok",
                "transport_proven=true",
                '"transport_proven": true',
                '"callback_received": true',
                '"ready": true',
            ):
                if marker in text:
                    positive.append(f"text_witness:{marker}")

    for path in required:
        try:
            value = _resolve(payload, path)
        except KeyError:
            reasons.append(f"required_missing:{path}")
            continue
        if not _is_truthy_semantic(value):
            reasons.append(f"required_not_proven:{path}")
        else:
            positive.append(f"required_proven:{path}")

    technical_ok = all(code == 0 for _, code in technical_codes) if technical_codes else None
    semantic_ok = not reasons and bool(positive)
    if not reasons and not positive:
        reasons.append("semantic_evidence_missing")
        semantic_ok = False

    return {
        "technical_ok": technical_ok,
        "semantic_ok": semantic_ok,
        "classification": "SEMANTIC_OK" if semantic_ok else "SEMANTIC_FAILED",
        "reasons": sorted(set(reasons)),
        "witnesses": sorted(set(positive)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="RDC semantic outcome guard")
    parser.add_argument("--input", type=Path, help="JSON de resultado; stdin quando omitido")
    parser.add_argument("--require", action="append", default=[])
    args = parser.parse_args()
    try:
        raw = args.input.read_text(encoding="utf-8") if args.input else sys.stdin.read()
        payload = json.loads(raw)
        result = evaluate(payload, tuple(args.require))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(json.dumps({"classification": "SEMANTIC_INVALID", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result["semantic_ok"] else 3


if __name__ == "__main__":
    raise SystemExit(main())

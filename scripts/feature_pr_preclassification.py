#!/usr/bin/env python3
"""Only PRE-classify feature vs repair; never produce READY_FOR_PR.

The provided facts MUST be verified by an external trusted controller. This
module validates schema and basic consistency, not source authenticity.
"""
from __future__ import annotations

import re

CONTRACT = "feature-repair-preclassification-v1"
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
FIELDS = frozenset({
    "contract", "kind", "issue_declared_kind", "kind_locked_before_execution",
    "capability_absent_at_base", "baseline_failure_reproduced",
    "prior_repair_blocked", "source_mode", "model_calls", "model_digest",
    "base_sha", "current_base_sha",
})


class ClassificationBlocked(ValueError):
    """Return only fixed failure codes, never untrusted input."""


def preclassify(payload: dict) -> dict:
    """Check logical consistency only; no authoritative admission decision."""
    if type(payload) is not dict or set(payload) != FIELDS:
        raise ClassificationBlocked("classification_schema_invalid")
    if payload["contract"] != CONTRACT:
        raise ClassificationBlocked("contract_invalid")
    kind = payload["kind"]
    if kind not in ("feature", "repair") or payload["issue_declared_kind"] != kind:
        raise ClassificationBlocked("kind_mismatch")
    if payload["kind_locked_before_execution"] is not True:
        raise ClassificationBlocked("kind_lock_unverified")
    if any(type(payload[key]) is not bool for key in (
        "capability_absent_at_base", "baseline_failure_reproduced", "prior_repair_blocked"
    )):
        raise ClassificationBlocked("baseline_schema_invalid")
    for key in ("base_sha", "current_base_sha"):
        if type(payload[key]) is not str or not SHA.fullmatch(payload[key]):
            raise ClassificationBlocked("sha_invalid")
    if payload["current_base_sha"] != payload["base_sha"]:
        raise ClassificationBlocked("base_moved")
    source = payload["source_mode"]
    if source not in ("assisted", "model_builder"):
        raise ClassificationBlocked("source_mode_invalid")
    calls = payload["model_calls"]
    if type(calls) is not int or not 0 <= calls <= 10000:
        raise ClassificationBlocked("calls_invalid")
    digest = payload["model_digest"]
    if source == "model_builder":
        if calls < 1 or type(digest) is not str or not DIGEST.fullmatch(digest):
            raise ClassificationBlocked("model_provenance_missing")
    elif calls != 0 or digest is not None:
        raise ClassificationBlocked("model_claim_mismatch")
    if kind == "repair":
        if payload["capability_absent_at_base"] is not False or \
           payload["baseline_failure_reproduced"] is not True or \
           source != "model_builder":
            raise ClassificationBlocked("repair_requires_real_model_failure")
    else:
        if payload["prior_repair_blocked"] is True:
            raise ClassificationBlocked("repair_reclassification_forbidden")
        if payload["capability_absent_at_base"] is not True or \
           payload["baseline_failure_reproduced"] is not False:
            raise ClassificationBlocked("feature_baseline_invalid")
    return {
        "contract": CONTRACT,
        "kind": kind,
        "classification_status": "schema_validated_only",
        "independent_evidence_verified": False,
        "ready_for_pr": False,
        "ready_for_merge": False,
        "host_execution_authorized": False,
    }

#!/usr/bin/env python3
"""Dormant, fail-closed coordinator for a future governed preservation action.

This is not a functional backup tool. It has no Render adapter, credential broker,
subprocess fallback, or trusted evidence provider. Those host-specific components
must be separately approved and injected. Activation also remains blocked until a
durable recovery mechanism can remove temporary access after abrupt process or host
termination.
"""
from __future__ import annotations

import ipaddress
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

SENSITIVE_ARGUMENT = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|postgres(?:ql)?://)"
)


class PreservationBlocked(RuntimeError):
    """Raised when mandatory safety configuration is absent or inconsistent."""


class AccessCleanupFailed(RuntimeError):
    """Raised when temporary access could not be proven removed."""


@dataclass(frozen=True)
class PreservationPlan:
    enabled: bool
    requested_allowlist_ip: str
    encrypted_output: Path


@dataclass(frozen=True)
class TrustedPreflightEvidence:
    """Evidence supplied by an approved host adapter, never by action payload."""

    approval_is_current: bool
    credential_channel_is_ready: bool
    observed_desktop_ip: str


def reject_sensitive_arguments(arguments: Sequence[str]) -> None:
    if any(SENSITIVE_ARGUMENT.search(str(argument)) for argument in arguments):
        raise PreservationBlocked("sensitive_argument_rejected")


def validate_plan(plan: PreservationPlan, evidence: TrustedPreflightEvidence) -> str:
    if type(plan.enabled) is not bool or plan.enabled is not True:
        raise PreservationBlocked("preservation_action_not_enabled")
    if type(evidence.approval_is_current) is not bool or evidence.approval_is_current is not True:
        raise PreservationBlocked("trusted_approval_missing")
    if (
        type(evidence.credential_channel_is_ready) is not bool
        or evidence.credential_channel_is_ready is not True
    ):
        raise PreservationBlocked("approved_credential_channel_missing")
    if not isinstance(evidence.observed_desktop_ip, str) or not isinstance(
        plan.requested_allowlist_ip, str
    ):
        raise PreservationBlocked("desktop_ip_invalid")
    try:
        observed = ipaddress.ip_address(evidence.observed_desktop_ip)
        requested = ipaddress.ip_address(plan.requested_allowlist_ip)
    except ValueError as exc:
        raise PreservationBlocked("desktop_ip_invalid") from exc
    if observed.version != 4 or requested.version != 4 or observed != requested:
        raise PreservationBlocked("desktop_ip_mismatch")
    if not isinstance(plan.encrypted_output, Path) or not plan.encrypted_output.is_absolute():
        raise PreservationBlocked("encrypted_output_must_be_absolute")
    return f"{observed}/32"


def preserve(
    plan: PreservationPlan,
    *,
    arguments: Sequence[str],
    load_trusted_evidence: Callable[[], TrustedPreflightEvidence],
    add_access: Callable[[str], None],
    export_encrypted: Callable[[], None],
    remove_access: Callable[[str], None],
    access_is_removed: Callable[[str], bool],
    validate_offline_restore: Callable[[], None],
) -> None:
    """Coordinate unit-testable adapters; no real adapter is provided here."""
    reject_sensitive_arguments(arguments)
    cidr = validate_plan(plan, load_trusted_evidence())
    access_attempted = False
    primary_error: BaseException | None = None
    try:
        access_attempted = True
        add_access(cidr)
        export_encrypted()
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if access_attempted:
            try:
                remove_access(cidr)
                if access_is_removed(cidr) is not True:
                    raise AccessCleanupFailed("temporary_access_removal_unproven")
            except BaseException as cleanup_error:
                if primary_error is not None:
                    raise AccessCleanupFailed(
                        "temporary_access_cleanup_failed_after_export_error"
                    ) from cleanup_error
                raise AccessCleanupFailed("temporary_access_cleanup_failed") from cleanup_error
    validate_offline_restore()


def main(arguments: Sequence[str] | None = None) -> int:
    try:
        reject_sensitive_arguments(list(sys.argv[1:] if arguments is None else arguments))
    except PreservationBlocked:
        reason = "sensitive_argument_rejected"
    else:
        reason = "host_specific_adapters_trusted_evidence_and_recovery_not_configured"
    print(json.dumps({"result": "blocked", "reason": reason}, sort_keys=True))
    return 20


if __name__ == "__main__":
    raise SystemExit(main())

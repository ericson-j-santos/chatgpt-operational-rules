#!/usr/bin/env python3
"""Fail-closed coordinator for a future governed Render PostgreSQL preservation action.

This module deliberately contains no credential broker, Render client, or subprocess
fallback. Host-specific adapters must be approved and injected by the governed action.
"""
from __future__ import annotations

import ipaddress
import json
import re
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
    approved_credential_channel: bool
    observed_desktop_ip: str
    requested_allowlist_ip: str
    encrypted_output: Path


def reject_sensitive_arguments(arguments: Sequence[str]) -> None:
    if any(SENSITIVE_ARGUMENT.search(str(argument)) for argument in arguments):
        raise PreservationBlocked("sensitive_argument_rejected")


def validate_plan(plan: PreservationPlan) -> str:
    if not plan.enabled:
        raise PreservationBlocked("preservation_action_not_enabled")
    if not plan.approved_credential_channel:
        raise PreservationBlocked("approved_credential_channel_missing")
    try:
        observed = ipaddress.ip_address(plan.observed_desktop_ip)
        requested = ipaddress.ip_address(plan.requested_allowlist_ip)
    except ValueError as exc:
        raise PreservationBlocked("desktop_ip_invalid") from exc
    if observed.version != 4 or requested.version != 4 or observed != requested:
        raise PreservationBlocked("desktop_ip_mismatch")
    if not plan.encrypted_output.is_absolute():
        raise PreservationBlocked("encrypted_output_must_be_absolute")
    return f"{observed}/32"


def preserve(
    plan: PreservationPlan,
    *,
    add_access: Callable[[str], None],
    export_encrypted: Callable[[], None],
    remove_access: Callable[[str], None],
    access_is_removed: Callable[[str], bool],
    validate_offline_restore: Callable[[], None],
) -> None:
    """Run export with mandatory compensation, then restore only after access removal."""
    cidr = validate_plan(plan)
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
                if not access_is_removed(cidr):
                    raise AccessCleanupFailed("temporary_access_removal_unproven")
            except BaseException as cleanup_error:
                if primary_error is not None:
                    raise AccessCleanupFailed("temporary_access_cleanup_failed_after_export_error") from cleanup_error
                raise AccessCleanupFailed("temporary_access_cleanup_failed") from cleanup_error
    validate_offline_restore()


def main() -> int:
    print(json.dumps({
        "result": "blocked",
        "reason": "host_specific_adapters_and_approved_credential_channel_not_configured",
    }, sort_keys=True))
    return 20


if __name__ == "__main__":
    raise SystemExit(main())

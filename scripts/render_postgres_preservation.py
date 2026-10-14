#!/usr/bin/env python3
"""Dormant, fail-closed coordinator for a future governed preservation action.

This is not a functional backup tool. It has no Render adapter, credential broker,
subprocess fallback, or trusted evidence provider. Those host-specific components
must be separately approved and injected. Activation also remains blocked until a
host supervisor can resume cleanup from the durable journal after abrupt process or
host termination.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import sys
import tempfile
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


@dataclass(frozen=True)
class ArtifactEvidence:
    path: str
    size: int
    sha256: str


class CleanupJournal:
    """Secret-free durable marker consumed by a future approved cleanup supervisor."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise PreservationBlocked("cleanup_journal_must_be_absolute")
        self.path = path

    def _read(self) -> dict[str, object] | None:
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PreservationBlocked("cleanup_journal_invalid") from exc
        if (
            payload.get("version") != 1
            or payload.get("status") != "cleanup_pending"
            or not isinstance(payload.get("cidr"), str)
        ):
            raise PreservationBlocked("cleanup_journal_invalid")
        try:
            network = ipaddress.ip_network(str(payload["cidr"]), strict=True)
        except ValueError as exc:
            raise PreservationBlocked("cleanup_journal_invalid") from exc
        if network.version != 4 or network.prefixlen != 32:
            raise PreservationBlocked("cleanup_journal_invalid")
        return payload

    def pending_cidr(self) -> str | None:
        payload = self._read()
        return None if payload is None else str(payload["cidr"])

    def mark_pending(self, cidr: str) -> None:
        try:
            network = ipaddress.ip_network(cidr, strict=True)
        except (TypeError, ValueError) as exc:
            raise PreservationBlocked("cleanup_cidr_invalid") from exc
        if network.version != 4 or network.prefixlen != 32:
            raise PreservationBlocked("cleanup_cidr_invalid")
        cidr = str(network)
        existing = self.pending_cidr()
        if existing is not None and existing != cidr:
            raise PreservationBlocked("different_cleanup_already_pending")
        if existing == cidr:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "status": "cleanup_pending", "cidr": cidr}
        fd, temporary = tempfile.mkstemp(prefix=self.path.name + ".", dir=self.path.parent)
        temporary_path = Path(temporary)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                temporary_path.chmod(0o600)
            except OSError:
                pass
            os.replace(temporary_path, self.path)
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    def clear(self, cidr: str) -> None:
        if self.pending_cidr() != cidr:
            raise AccessCleanupFailed("cleanup_journal_does_not_match")
        try:
            self.path.unlink()
        except OSError as exc:
            raise AccessCleanupFailed("cleanup_journal_clear_failed") from exc


def reject_sensitive_arguments(arguments: Sequence[str]) -> None:
    if any(SENSITIVE_ARGUMENT.search(str(argument)) for argument in arguments):
        raise PreservationBlocked("sensitive_argument_rejected")


def validate_encrypted_artifact(path: Path) -> ArtifactEvidence:
    if not isinstance(path, Path) or not path.is_absolute():
        raise PreservationBlocked("encrypted_output_must_be_absolute")
    try:
        stat = path.stat()
    except OSError as exc:
        raise PreservationBlocked("encrypted_artifact_unavailable") from exc
    if not path.is_file() or stat.st_size < 1:
        raise PreservationBlocked("encrypted_artifact_empty")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise PreservationBlocked("encrypted_artifact_unreadable") from exc
    return ArtifactEvidence(path=str(path), size=stat.st_size, sha256=digest.hexdigest())


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
    cleanup_journal: CleanupJournal,
    add_access: Callable[[str], None],
    export_encrypted: Callable[[], None],
    remove_access: Callable[[str], None],
    access_is_removed: Callable[[str], bool],
    validate_offline_restore: Callable[[ArtifactEvidence], None],
    verify_artifact: Callable[[Path], ArtifactEvidence] = validate_encrypted_artifact,
) -> ArtifactEvidence:
    """Coordinate unit-testable adapters; no real adapter is provided here."""
    reject_sensitive_arguments(arguments)
    cidr = validate_plan(plan, load_trusted_evidence())
    cleanup_journal.mark_pending(cidr)
    access_attempted = False
    primary_error: BaseException | None = None
    artifact: ArtifactEvidence | None = None
    try:
        access_attempted = True
        add_access(cidr)
        export_encrypted()
        artifact = verify_artifact(plan.encrypted_output)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if access_attempted:
            try:
                remove_access(cidr)
                if access_is_removed(cidr) is not True:
                    raise AccessCleanupFailed("temporary_access_removal_unproven")
                cleanup_journal.clear(cidr)
            except BaseException as cleanup_error:
                if primary_error is not None:
                    raise AccessCleanupFailed(
                        "temporary_access_cleanup_failed_after_export_error"
                    ) from cleanup_error
                raise AccessCleanupFailed("temporary_access_cleanup_failed") from cleanup_error
    if artifact is None:
        raise PreservationBlocked("encrypted_artifact_unavailable")
    validate_offline_restore(artifact)
    return artifact


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

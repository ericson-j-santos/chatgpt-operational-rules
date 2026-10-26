"""One narrowly scoped WSL *running-status* check for the existing Windows Gateway.

This is a core Gateway capability, not a Python child-process workaround.
The ordinary 'wsl' executable remains blocked for every other command.
Listing currently running distributions never executes a Linux command.
"""
from __future__ import annotations

import ctypes
import json
import ntpath
import os
import re
import socket
import subprocess
from typing import Any, Sequence

EXACT_FLAGS = ("--list", "--running", "--quiet")
POLICY_FLAG = "noteri_wsl_running_inventory"
DISTRO_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
UBUNTU_NAME = re.compile(r"Ubuntu(?:-[0-9]{2}\.[0-9]{2})?\Z", re.IGNORECASE)
MAX_BYTES = 4096
MAX_DISTROS = 16


class StatusBlocked(ValueError):
    """Constant reason codes only. Never include WSL output or names."""


def is_wsl_command(args: Sequence[str]) -> bool:
    return bool(args and isinstance(args[0], str)
                and ntpath.basename(args[0]).casefold() == "wsl.exe")


def host_context() -> tuple[str, str]:
    return os.name, socket.gethostname()


def system_wsl_path() -> str:
    """Resolve the actual Win32 system directory, never PATH or a caller path."""
    if os.name != "nt":
        raise StatusBlocked("WSL_WINDOWS_REQUIRED")
    try:
        library = ctypes.WinDLL("kernel32", use_last_error=True)
        method = library.GetSystemDirectoryW
        method.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
        method.restype = ctypes.c_uint32
        buffer = ctypes.create_unicode_buffer(32768)
        written = method(buffer, len(buffer))
        if not 3 <= written < len(buffer):
            raise StatusBlocked("WSL_SYSTEM_BINARY_UNVERIFIED")
        return ntpath.join(buffer.value, "wsl.exe")
    except (OSError, AttributeError, ValueError, TypeError):
        raise StatusBlocked("WSL_SYSTEM_BINARY_UNVERIFIED") from None


def validate_request(args: Sequence[str], risk: int, policy: dict[str, Any]) -> None:
    """Permit exactly one Windows WSL management query under explicit opt-in."""
    if not is_wsl_command(args):
        raise StatusBlocked("WSL_COMMAND_INVALID")
    platform, host = host_context()
    if (platform != "nt" or host.casefold() != "noteri"
            or policy.get("platform") != "windows"
            or policy.get(POLICY_FLAG) is not True):
        raise StatusBlocked("WSL_PROBE_NOT_AUTHORIZED")
    if risk != 1:
        raise StatusBlocked("WSL_PROBE_RISK_INVALID")
    if len(args) != 4 or tuple(args[1:]) != EXACT_FLAGS:
        raise StatusBlocked("WSL_PROBE_ARGUMENTS_DENIED")
    original = args[0]
    approved = system_wsl_path()
    if (not ntpath.isabs(original)
            or ntpath.normcase(ntpath.normpath(original)) !=
               ntpath.normcase(ntpath.normpath(approved))):
        raise StatusBlocked("WSL_PROBE_BINARY_UNTRUSTED")


def _decode(output: bytes) -> str:
    if type(output) is not bytes or len(output) > MAX_BYTES:
        raise StatusBlocked("WSL_PROBE_OUTPUT_INVALID")
    try:
        if output.startswith((b"\xff\xfe", b"\xfe\xff")):
            return output.decode("utf-16")
        if b"\x00" in output:
            return output.decode("utf-16le")
        return output.decode("utf-8-sig")
    except UnicodeError:
        raise StatusBlocked("WSL_PROBE_OUTPUT_INVALID") from None


def sanitize_output(returncode: int, stdout: bytes, stderr: bytes) -> dict[str, Any]:
    if type(returncode) is not int or returncode != 0:
        raise StatusBlocked("WSL_PROBE_FAILED")
    if type(stderr) is not bytes or len(stderr) > MAX_BYTES:
        raise StatusBlocked("WSL_PROBE_OUTPUT_INVALID")
    if _decode(stderr).strip():
        raise StatusBlocked("WSL_PROBE_STDERR_UNEXPECTED")
    lines = [value.strip() for value in _decode(stdout).splitlines() if value.strip()]
    if len(lines) > MAX_DISTROS or any(
        DISTRO_NAME.fullmatch(name) is None for name in lines
    ) or len(set(name.casefold() for name in lines)) != len(lines):
        raise StatusBlocked("WSL_PROBE_OUTPUT_INVALID")
    ubuntu_count = sum(UBUNTU_NAME.fullmatch(name) is not None for name in lines)
    return {
        "result": "NOTERI_WSL_RUNNING_STATUS_READONLY",
        "scope": "current_windows_token_running_distributions",
        "running_count": len(lines),
        "ubuntu_running_candidate": ubuntu_count == 1,
        "ubuntu_running_count": ubuntu_count,
        "distro_names_logged": False,
        "linux_command_executed": False,
        "linux_service_started": False,
        "gateway_posix_verified": False,
        "systemd_verified": False,
        "postgresql_service_verified": False,
        "postgresql_tls_verified": False,
        "authority_ready": False,
        "ready_for_pr": False,
    }


def run_fixed(
    args: Sequence[str], risk: int, policy: dict[str, Any], *,
    cwd: str, timeout: int, spawn_options: dict[str, Any] | None = None,
    runner=None,
) -> subprocess.CompletedProcess[str]:
    """Called ONLY inside Gateway's session enforcement, repo lock and SHA gate."""
    validate_request(args, risk, policy)
    method = subprocess.run if runner is None else runner
    try:
        outcome = method(
            list(args), cwd=cwd, capture_output=True, check=False,
            shell=False, timeout=min(timeout, 12),
            **(spawn_options or {}),
        )
        observed = sanitize_output(outcome.returncode, outcome.stdout, outcome.stderr)
        return subprocess.CompletedProcess(
            list(args), 0, json.dumps(observed, sort_keys=True), "",
        )
    except (StatusBlocked, OSError, subprocess.TimeoutExpired) as exc:
        reason = str(exc) if isinstance(exc, StatusBlocked) else "WSL_PROBE_UNAVAILABLE"
        if reason not in {
            "WSL_PROBE_FAILED", "WSL_PROBE_OUTPUT_INVALID",
            "WSL_PROBE_STDERR_UNEXPECTED", "WSL_PROBE_UNAVAILABLE",
        }:
            reason = "WSL_PROBE_UNAVAILABLE"
        return subprocess.CompletedProcess(
            list(args), 22,
            json.dumps({"result": "NOTERI_WSL_RUNNING_STATUS_BLOCKED",
                        "reason": reason, "ready_for_pr": False},
                       sort_keys=True), "",
        )

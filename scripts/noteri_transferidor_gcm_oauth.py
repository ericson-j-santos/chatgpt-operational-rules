#!/usr/bin/env python3
"""One scoped, human-consented GitHub OAuth initiation for TRANSF-008.

This is a Risk 3 action. It must be invoked ONLY through the existing
Session Launcher, Command Gateway and an exact, time-bound owner Risk 3
allowlist entry. No browser automation, token handling or secret output.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Callable, Any

TARGET_URL = "https://github.com/ericson-j-santos/transferidor-documental.git"
TARGET_REF = "refs/heads/import/transf008-v0.4.0"
TARGET_SHA = "4bbcb6575773ae3e7ff7d54a81d7b3bea802c556"
CONFIRM = "INITIATE-TRANSF008-NOTERI-GCM-OAUTH"
PROFILE_SUFFIX = Path("ReqSys") / "TodoGlobal24x7" / "host-profile.json"
POSSIBLE_SECRET_ENV = (
    "GH_TOKEN", "GITHUB_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GH_ENTERPRISE_TOKEN",
    "GIT_ASKPASS", "SSH_ASKPASS", "GCM_TRACE", "GCM_TRACE_SECRETS", "GIT_TRACE",
    "GIT_TRACE_PACKET", "GIT_TRACE_CURL", "GIT_TRACE_CURL_NO_DATA", "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_EXEC_PATH",
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def receipt(status: str, *, browser_invoked: bool = False, readback: bool = False) -> dict[str, Any]:
    return {"schema_version": "1", "task_id": "TRANSF-008", "host": "Noteri",
            "status": status, "source_sha": TARGET_SHA, "oauth_browser_requested": browser_invoked,
            "source_head_verified": readback, "secret_value_exposed": False}


def no_secret_environment(source: dict[str, str]) -> dict[str, str]:
    env = dict(source)
    for key in list(env):
        if key.upper() in POSSIBLE_SECRET_ENV or key.upper().startswith("GIT_CONFIG_KEY_") or key.upper().startswith("GIT_CONFIG_VALUE_"):
            env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env



def classify_gcm_stderr(raw: str | None) -> str:
    """Only fixed error categories leave this process; raw diagnostic text never does."""
    if not isinstance(raw, str):
        return "OAUTH_NOT_COMPLETED"
    message = raw[:8192].casefold()
    if any(x in message for x in ("unrecognized command", "unrecognized option",
                                  "unknown option", "unrecognized argument")):
        return "OAUTH_GCM_ARGUMENT_UNSUPPORTED"
    if ("browser" in message or "gui" in message) and any(
        x in message for x in ("not available", "not supported", "unable to open",
                             "failed to launch", "could not launch",
                             "no default browser", "cannot start")):
        return "OAUTH_BROWSER_UNAVAILABLE"
    if any(x in message for x in ("operation canceled", "operation cancelled",
                                  "user canceled", "user cancelled")):
        return "OAUTH_CONSENT_CANCELLED"
    if any(x in message for x in ("certificate verify failed", "ssl handshake failed",
                                  "proxy authentication required", "could not resolve host")):
        return "OAUTH_NETWORK_OR_TLS_BLOCKED"
    if any(x in message for x in ("access is denied", "permission denied",
                                  "credential store is unavailable")):
        return "OAUTH_LOCAL_CREDENTIAL_STORE_BLOCKED"
    return "OAUTH_NOT_COMPLETED"

def profile_ok(*, os_name: str, hostname: str, localappdata: str | None) -> bool:
    if os_name != "nt" or hostname.casefold() != "noteri" or not localappdata:
        return False
    path = Path(localappdata) / PROFILE_SUFFIX
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 8192:
            return False
        obj = json.loads(path.read_text(encoding="utf-8"))
        return (isinstance(obj, dict) and obj.get("host", "").casefold() == "noteri"
                and obj.get("profile") == "NORMAL"
                and obj.get("accepts_new_development") is True)
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def safe_check_head(git: str, *, runner: Callable[..., Any], env: dict[str, str]) -> bool:
    guarded_env = {**env, "GCM_INTERACTIVE": "never"}
    args = [git, "-c", "credential.interactive=never", "ls-remote", "--heads", TARGET_URL, TARGET_REF]
    try:
        cp = runner(args, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                    timeout=20, check=False, shell=False, env=guarded_env)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if cp.returncode != 0 or len(cp.stdout) > 512:
        return False
    rows = cp.stdout.splitlines()
    if len(rows) != 1:
        return False
    parts = rows[0].split("\t")
    return len(parts) == 2 and SHA_RE.fullmatch(parts[0]) is not None and parts == [TARGET_SHA, TARGET_REF]


def run(*, confirm: str, preflight_only: bool, os_name: str, hostname: str,
        localappdata: str | None, environment: dict[str, str], git: str | None,
        runner: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    if not profile_ok(os_name=os_name, hostname=hostname, localappdata=localappdata):
        return receipt("BLOCKED_HOST_OR_PROFILE")
    if not git:
        return receipt("BLOCKED_GIT_UNAVAILABLE")
    env = no_secret_environment(environment)
    if safe_check_head(git, runner=runner, env=env):
        return receipt("SOURCE_AUTH_READY", readback=True)
    if preflight_only:
        return receipt("OAUTH_HUMAN_CONSENT_REQUIRED")
    if confirm != CONFIRM:
        return receipt("BLOCKED_CONFIRMATION")

    # Risk 3 consent must be exact. Re-authenticate explicitly when a prior
    # account record exists but Git SHA-bound readback is unavailable.
    # This can replace a cached login; never execute from read-only probes.
    # GCM itself handles the OAuth browser and the Windows Credential Manager.
    # GCM stdout is discarded; diagnostic stderr stays only in transient memory.
    # Neither raw stderr nor credentials are written to logs, files or receipts.
    try:
        cp = runner([git, "credential-manager", "github", "login", "--browser",
                     "--url", "https://github.com", "--force"], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace",
                    timeout=120, check=False, shell=False,
                    env={**env, "GCM_INTERACTIVE": "always"})
    except (OSError, subprocess.TimeoutExpired):
        return receipt("OAUTH_BROWSER_UNAVAILABLE", browser_invoked=True)
    if cp.returncode != 0:
        # Categorize only in memory; do not emit any raw GCM diagnostic content.
        return receipt(classify_gcm_stderr(getattr(cp, "stderr", None)), browser_invoked=True)
    if not safe_check_head(git, runner=runner, env=env):
        return receipt("BLOCKED_PRIVATE_REPO_READBACK", browser_invoked=True)
    return receipt("SOURCE_AUTH_READY", browser_invoked=True, readback=True)



# The generic owner Risk 3 gateway intentionally drops child stdout on failure.
# Fixed numeric exit codes preserve fail-closed behavior without leaking GCM text.
GCM_FAILURE_EXIT_CODES = {
    "OAUTH_GCM_ARGUMENT_UNSUPPORTED": 31,
    "OAUTH_BROWSER_UNAVAILABLE": 32,
    "OAUTH_CONSENT_CANCELLED": 33,
    "OAUTH_NETWORK_OR_TLS_BLOCKED": 34,
    "OAUTH_LOCAL_CREDENTIAL_STORE_BLOCKED": 35,
    "OAUTH_NOT_COMPLETED": 36,
    "BLOCKED_PRIVATE_REPO_READBACK": 37,
}


def exit_for_status(status: str, *, preflight_only: bool) -> int:
    if status == "SOURCE_AUTH_READY":
        return 0
    if preflight_only and status == "OAUTH_HUMAN_CONSENT_REQUIRED":
        return 0
    return GCM_FAILURE_EXIT_CODES.get(status, 28)


def diagnose_readback(*, git: str, runner: Callable[..., Any],
                      env: dict[str, str]) -> dict[str, Any]:
    """Read fixed Git reference once without prompting; return only enums."""
    args = [git, "-c", "credential.interactive=never", "ls-remote",
            "--heads", TARGET_URL, TARGET_REF]
    guarded = {**env, "GCM_INTERACTIVE": "never"}
    try:
        cp = runner(args, stdin=subprocess.DEVNULL, capture_output=True,
                    text=True, timeout=20, check=False, shell=False, env=guarded)
    except (OSError, subprocess.TimeoutExpired):
        return receipt("READBACK_TRANSPORT_UNAVAILABLE")
    if cp.returncode != 0:
        # Never return or persist stderr, which may contain URLs or identity.
        error = str(getattr(cp, "stderr", "") or "")[:8192].casefold()
        if any(x in error for x in ("could not read username", "terminal prompts disabled",
                                     "credentials unavailable", "no credentials",
                                     "unable to get password", "credential unavailable")):
            return receipt("READBACK_CREDENTIAL_UNAVAILABLE")
        if any(x in error for x in ("authentication failed", "http 401",
                                     "http 403", "permission denied", "access denied")):
            return receipt("READBACK_AUTH_OR_PERMISSION_DENIED")
        if "repository not found" in error:
            return receipt("READBACK_PRIVATE_REPOSITORY_NOT_ACCESSIBLE")
        if any(x in error for x in ("ssl certificate", "certificate verify",
                                     "tls handshake", "proxy authentication")):
            return receipt("READBACK_TLS_OR_PROXY_BLOCKED")
        if any(x in error for x in ("could not resolve host", "failed to connect",
                                     "network is unreachable", "connection timed out")):
            return receipt("READBACK_NETWORK_BLOCKED")
        return receipt("READBACK_TRANSPORT_UNCLASSIFIED")
    output = str(getattr(cp, "stdout", "") or "")
    if len(output) > 512:
        return receipt("READBACK_INVALID_RESPONSE")
    rows = output.splitlines()
    if not rows:
        return receipt("READBACK_REF_ABSENT")
    if len(rows) != 1:
        return receipt("READBACK_INVALID_RESPONSE")
    parts = rows[0].split("\t")
    if len(parts) != 2 or parts[1] != TARGET_REF or SHA_RE.fullmatch(parts[0]) is None:
        return receipt("READBACK_INVALID_RESPONSE")
    if parts[0] != TARGET_SHA:
        return receipt("READBACK_SHA_MISMATCH")
    return receipt("SOURCE_AUTH_READY", readback=True)

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", default="")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--diagnose-readback", action="store_true")
    args = parser.parse_args()
    if args.diagnose_readback:
        if args.preflight_only or args.confirm:
            result = receipt("BLOCKED_AMBIGUOUS_DIAGNOSTIC")
        elif not profile_ok(os_name=os.name, hostname=socket.gethostname(),
                            localappdata=os.environ.get("LOCALAPPDATA")):
            result = receipt("BLOCKED_HOST_OR_PROFILE")
        elif not shutil.which("git"):
            result = receipt("BLOCKED_GIT_UNAVAILABLE")
        else:
            result = diagnose_readback(git=shutil.which("git"),
                                       runner=subprocess.run,
                                       env=no_secret_environment(os.environ))
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        # The diagnostic's exit 0 means classification completed, NOT authenticated.
        return 0 if result["status"].startswith("READBACK_") or result["status"] == "SOURCE_AUTH_READY" else 28
    result = run(confirm=args.confirm, preflight_only=args.preflight_only,
                 os_name=os.name, hostname=socket.gethostname(),
                 localappdata=os.environ.get("LOCALAPPDATA"), environment=os.environ,
                 git=shutil.which("git"))
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return exit_for_status(result["status"], preflight_only=args.preflight_only)


if __name__ == "__main__":
    raise SystemExit(main())

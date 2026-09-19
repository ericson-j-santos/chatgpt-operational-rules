#!/usr/bin/env python3
"""Launch Git Credential Manager GitHub device-flow without exposing the resulting token.

The GitHub credential is stored by GCM in the operating-system credential store.
This helper only mirrors GCM's human authorization instructions to a temporary
text file so the owner can approve the OAuth flow. It never calls
`git credential fill` and never reads the stored credential.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", required=True)
    parser.add_argument("--result-file", required=True)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()

    if args.confirm != "AUTHORIZE-GITHUB-GCM-DEVICE":
        print("BLOCKED: invalid confirmation", file=sys.stderr)
        return 2

    result_file = Path(args.result_file).resolve()
    result_file.parent.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"

    # GCM owns the OAuth flow and secure credential persistence.
    # We only stream the device-flow instructions; no access token is read here.
    proc = subprocess.Popen(
        [
            "git",
            "credential-manager",
            "github",
            "login",
            "--device",
            "--force",
            "--no-ui",
            "--username",
            args.username,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        bufsize=1,
    )

    with result_file.open("w", encoding="utf-8", newline="\n") as handle:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            lowered = line.casefold()
            # Defensive redaction: device instructions are allowed; tokens/passwords are not.
            if any(marker in lowered for marker in ("access_token", "refresh_token", "password=", "authorization:")):
                line = "[REDACTED]"
            handle.write(line + "\n")
            handle.flush()

    code = proc.wait()
    with result_file.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"\nGCM_EXIT={code}\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

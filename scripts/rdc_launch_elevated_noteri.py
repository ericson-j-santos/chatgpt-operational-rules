from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--correlation-id", required=True)
    args = parser.parse_args()

    target = Path(__file__).resolve().with_name("rdc_noteri_elevated_provision.py")
    if not target.is_file():
        print(json.dumps({"result": "blocked", "reason": "target_missing", "secret_value_exposed": False}))
        return 2

    parameters = subprocess.list2cmdline([str(target), "--correlation-id", args.correlation_id])
    rc = ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        sys.executable,
        parameters,
        str(target.parent),
        1,
    )
    ok = int(rc) > 32
    print(json.dumps({
        "result": "uac_prompt_dispatched" if ok else "blocked",
        "shell_execute_code": int(rc),
        "secret_value_exposed": False,
    }, sort_keys=True))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())

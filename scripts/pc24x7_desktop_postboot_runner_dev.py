from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

BASE = Path(os.environ["LOCALAPPDATA"]) / "ReqSys" / "TodoGlobal24x7"
LOCK = BASE / "postboot-runner.lock"
LOG = BASE / "autostart.log"
VALIDATOR = BASE / "postboot_validate.py"


def log(message: str) -> None:
    stamp = datetime.now(UTC).isoformat()
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": stamp, "message": message}) + "\n")


def main() -> int:
    now = time.time()
    if LOCK.exists() and now - LOCK.stat().st_mtime < 900:
        log("skip: another postboot runner is active")
        return 0
    LOCK.write_text(str(os.getpid()), encoding="ascii")
    try:
        log("start")
        cp = subprocess.run(
            [sys.executable, str(VALIDATOR)],
            capture_output=True,
            text=True,
            timeout=420,
            check=False,
        )
        log(
            f"finish rc={cp.returncode} "
            f"stdout_tail={cp.stdout[-500:].strip()} "
            f"stderr_tail={cp.stderr[-500:].strip()}"
        )
        return int(cp.returncode)
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())

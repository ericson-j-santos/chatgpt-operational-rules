from __future__ import annotations

import json
import re
from pathlib import Path

LOG = Path(r"C:\ProgramData\ReqSys\RdcSvc\rdc-headless.log")

SAFE_PATTERNS = [
    re.compile(r"^.*child_exit code=([^ ]+) signal=([^\r\n]+).*$", re.I),
    re.compile(r"^.*(SIGTERM|SIGINT|SIGHUP) received.*$", re.I),
    re.compile(r"^.*Remote session expired.*$", re.I),
    re.compile(r"^.*This device is now offline for remote calls.*$", re.I),
    re.compile(r"^.*Device startup failed.*$", re.I),
    re.compile(r"^.*Persisted session invalid.*$", re.I),
]


def main() -> int:
    result: dict[str, object] = {
        "log_present": LOG.is_file(),
        "events": [],
        "secret_value_exposed": False,
    }
    if LOG.is_file():
        lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        events: list[dict[str, object]] = []
        for line in lines:
            for pattern in SAFE_PATTERNS:
                match = pattern.match(line)
                if not match:
                    continue
                if "child_exit" in line:
                    events.append({
                        "type": "child_exit",
                        "code": match.group(1),
                        "signal": match.group(2),
                    })
                elif "received" in line and match.groups():
                    events.append({"type": "signal", "signal": match.group(1).upper()})
                elif "Remote session expired" in line:
                    events.append({"type": "session_expired"})
                elif "offline for remote calls" in line:
                    events.append({"type": "remote_offline"})
                elif "Device startup failed" in line:
                    events.append({"type": "startup_failed"})
                elif "Persisted session invalid" in line:
                    events.append({"type": "persisted_invalid"})
                break
        result["events"] = events
        result["event_count"] = len(events)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

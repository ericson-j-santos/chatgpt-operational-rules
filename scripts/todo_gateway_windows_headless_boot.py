#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


def interactive_session_present() -> bool:
    cp = subprocess.run(["query", "user"], capture_output=True, text=True, timeout=15, check=False)
    text = ((cp.stdout or "") + "\n" + (cp.stderr or "")).lower()
    return any(token in text for token in (" console ", " rdp-tcp"))


def publish(result: dict, evidence_path: Path, beacon_host: str | None, beacon_port: int) -> None:
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if not beacon_host:
        return
    try:
        with socket.create_connection((beacon_host, beacon_port), timeout=5) as conn:
            conn.sendall(json.dumps(result).encode("utf-8"))
        result["beacon_sent"] = True
    except Exception as exc:
        result["beacon_sent"] = False
        result["beacon_error"] = type(exc).__name__


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", default=str(Path.home() / "AppData/Local/ReqSys/TodoGlobal24x7"))
    parser.add_argument("--beacon-host")
    parser.add_argument("--beacon-port", type=int, default=19194)
    args = parser.parse_args()

    base = Path(args.runtime_dir)
    postboot = base / "postboot_runner.py"
    postboot_evidence = base / "evidence" / "postboot-last.json"
    evidence_path = base / "evidence" / "headless-boot-last.json"
    result = {"generated_at": datetime.now(UTC).isoformat(), "contract": "todo-global-headless-boot-v2"}

    if interactive_session_present():
        result.update({"interactive_session_present": True, "ready": False, "status": "blocked_interactive_session"})
        publish(result, evidence_path, args.beacon_host, args.beacon_port)
        return 10

    result["interactive_session_present"] = False
    proc = subprocess.Popen([sys.executable, str(postboot)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    while proc.poll() is None:
        if interactive_session_present():
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            result.update({"interactive_session_appeared": True, "ready": False, "status": "interactive_session_appeared"})
            publish(result, evidence_path, args.beacon_host, args.beacon_port)
            return 11
        time.sleep(2)

    proc.communicate(timeout=5)
    result["postboot_rc"] = proc.returncode
    inner = json.loads(postboot_evidence.read_text(encoding="utf-8")) if postboot_evidence.is_file() else {}
    result["postboot_ready"] = bool(inner.get("ready"))
    result["interactive_session_appeared"] = False
    result["ready"] = proc.returncode == 0 and result["postboot_ready"]
    result["status"] = "ready" if result["ready"] else "postboot_failed"
    result["completed_at"] = datetime.now(UTC).isoformat()
    publish(result, evidence_path, args.beacon_host, args.beacon_port)
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

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
    if beacon_host:
        try:
            with socket.create_connection((beacon_host, beacon_port), timeout=5) as conn:
                conn.sendall(json.dumps(result).encode("utf-8"))
            result["beacon_sent"] = True
        except Exception as exc:
            result["beacon_sent"] = False
            result["beacon_error"] = type(exc).__name__
    evidence_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


def run_fail_closed(command: list[str], *, cwd: Path | None = None) -> tuple[int, str, str, bool]:
    proc = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    session_appeared = False
    while proc.poll() is None:
        if interactive_session_present():
            session_appeared = True
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            break
        time.sleep(2)
    stdout, stderr = proc.communicate(timeout=10)
    return int(proc.returncode or 0), stdout, stderr, session_appeared


def last_json(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", default=str(Path.home() / "AppData/Local/ReqSys/TodoGlobal24x7"))
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--beacon-host")
    parser.add_argument("--beacon-port", type=int, default=19194)
    args = parser.parse_args()

    base = Path(args.runtime_dir)
    repo_root = Path(args.repo_root)
    e2e_evidence = base / "evidence" / "e2e-last.json"
    evidence_path = base / "evidence" / "headless-boot-last.json"
    result = {
        "generated_at": datetime.now(UTC).isoformat(),
        "contract": "todo-global-headless-boot-v4",
        "resilience_proof_mode": "separate",
    }

    if interactive_session_present():
        result.update({"interactive_session_present": True, "ready": False, "status": "blocked_interactive_session"})
        publish(result, evidence_path, args.beacon_host, args.beacon_port)
        return 10
    if not repo_root.is_dir():
        result.update({"interactive_session_present": False, "ready": False, "status": "repo_root_missing"})
        publish(result, evidence_path, args.beacon_host, args.beacon_port)
        return 12

    result["interactive_session_present"] = False
    runtime_rc, runtime_stdout, _, session_appeared = run_fail_closed(
        [
            sys.executable,
            "scripts/todo_gateway_pc24x7.py",
            "up",
            "--wait-seconds",
            "45",
            "--docker-wait-seconds",
            "90",
        ],
        cwd=repo_root,
    )
    if session_appeared:
        result.update({"interactive_session_appeared": True, "ready": False, "status": "interactive_session_appeared"})
        publish(result, evidence_path, args.beacon_host, args.beacon_port)
        return 11

    runtime_data = last_json(runtime_stdout)
    result.update(
        {
            "runtime_rc": runtime_rc,
            "docker_ready": bool(runtime_data.get("docker_ready")),
            "runtime_ready": bool(runtime_data.get("ready")),
        }
    )
    if runtime_rc != 0 or not result["docker_ready"] or not result["runtime_ready"]:
        result.update({"interactive_session_appeared": False, "ready": False, "status": "runtime_failed"})
        publish(result, evidence_path, args.beacon_host, args.beacon_port)
        return 1

    e2e_rc, _, _, session_appeared = run_fail_closed(
        [sys.executable, "-m", "scripts.todo_gateway_pc24x7_e2e"], cwd=repo_root
    )
    if session_appeared:
        result.update({"interactive_session_appeared": True, "ready": False, "status": "interactive_session_appeared"})
        publish(result, evidence_path, args.beacon_host, args.beacon_port)
        return 11

    e2e_data = json.loads(e2e_evidence.read_text(encoding="utf-8")) if e2e_evidence.is_file() else {}
    result.update(
        {
            "e2e_rc": e2e_rc,
            "e2e_ready": bool(e2e_data.get("ready")),
            "e2e_event_id": e2e_data.get("event_id"),
            "e2e_request_id": e2e_data.get("request_id"),
            "interactive_session_appeared": False,
        }
    )
    result["ready"] = e2e_rc == 0 and result["e2e_ready"]
    result["status"] = "ready" if result["ready"] else "e2e_failed"
    result["completed_at"] = datetime.now(UTC).isoformat()
    publish(result, evidence_path, args.beacon_host, args.beacon_port)
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

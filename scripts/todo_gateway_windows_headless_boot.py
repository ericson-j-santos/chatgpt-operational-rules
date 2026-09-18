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

CONTRACT = "todo-global-headless-boot-v4"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def interactive_session_present() -> bool:
    cp = subprocess.run(["query", "user"], capture_output=True, text=True, timeout=15, check=False)
    text = ((cp.stdout or "") + "\n" + (cp.stderr or "")).lower()
    return any(token in text for token in (" console ", " rdp-tcp"))


def publish(
    result: dict,
    evidence_path: Path,
    beacon_host: str | None,
    beacon_port: int,
    *,
    stage: str,
    terminal: bool,
) -> None:
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(result)
    payload.update({"stage": stage, "terminal": terminal, "observed_at": now_iso()})
    if beacon_host:
        try:
            with socket.create_connection((beacon_host, beacon_port), timeout=5) as conn:
                conn.sendall(json.dumps(payload).encode("utf-8"))
            payload["beacon_sent"] = True
        except Exception as exc:
            payload["beacon_sent"] = False
            payload["beacon_error"] = type(exc).__name__
    evidence_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def terminate_process(proc: subprocess.Popen[str]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def run_fail_closed(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout_seconds: int,
) -> tuple[int, str, str, bool, bool]:
    proc = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    session_appeared = False
    timed_out = False
    deadline = time.monotonic() + timeout_seconds
    while proc.poll() is None:
        if interactive_session_present():
            session_appeared = True
            terminate_process(proc)
            break
        if time.monotonic() >= deadline:
            timed_out = True
            terminate_process(proc)
            break
        time.sleep(2)
    try:
        stdout, stderr = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate(timeout=10)
        timed_out = True
    return int(proc.returncode if proc.returncode is not None else -1), stdout, stderr, session_appeared, timed_out


def read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def tail(text: str, limit: int = 500) -> str:
    return (text or "").strip()[-limit:]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", default=str(Path.home() / "AppData/Local/ReqSys/TodoGlobal24x7"))
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--beacon-host")
    parser.add_argument("--beacon-port", type=int, default=19194)
    parser.add_argument("--postboot-timeout", type=int, default=480)
    parser.add_argument("--e2e-timeout", type=int, default=180)
    args = parser.parse_args()
    if args.postboot_timeout < 30 or args.e2e_timeout < 30:
        parser.error("timeouts must be >= 30 seconds")

    base = Path(args.runtime_dir)
    repo_root = Path(args.repo_root)
    active_repo_pointer = base / "active-repo-root.txt"
    if active_repo_pointer.is_file():
        pointed = Path(active_repo_pointer.read_text(encoding="utf-8").strip())
        if pointed.is_dir():
            repo_root = pointed
    postboot = base / "postboot_runner.py"
    postboot_evidence = base / "evidence" / "postboot-last.json"
    e2e_evidence = base / "evidence" / "e2e-last.json"
    evidence_path = base / "evidence" / "headless-boot-last.json"
    result = {"generated_at": now_iso(), "contract": CONTRACT}

    publish(result, evidence_path, args.beacon_host, args.beacon_port, stage="runner_started", terminal=False)
    if interactive_session_present():
        result.update({"interactive_session_present": True, "ready": False, "status": "blocked_interactive_session"})
        publish(result, evidence_path, args.beacon_host, args.beacon_port, stage="blocked_interactive_session", terminal=True)
        return 10
    if not repo_root.is_dir():
        result.update({"interactive_session_present": False, "ready": False, "status": "repo_root_missing"})
        publish(result, evidence_path, args.beacon_host, args.beacon_port, stage="repo_root_missing", terminal=True)
        return 12

    result["interactive_session_present"] = False
    publish(result, evidence_path, args.beacon_host, args.beacon_port, stage="postboot_started", terminal=False)
    postboot_rc, postboot_stdout, postboot_stderr, session_appeared, postboot_timed_out = run_fail_closed(
        [sys.executable, str(postboot)], timeout_seconds=args.postboot_timeout
    )
    if session_appeared:
        result.update({"interactive_session_appeared": True, "ready": False, "status": "interactive_session_appeared"})
        publish(result, evidence_path, args.beacon_host, args.beacon_port, stage="interactive_session_appeared", terminal=True)
        return 11
    if postboot_timed_out:
        result.update({
            "interactive_session_appeared": False,
            "postboot_rc": postboot_rc,
            "postboot_timed_out": True,
            "postboot_stdout_tail": tail(postboot_stdout),
            "postboot_stderr_tail": tail(postboot_stderr),
            "ready": False,
            "status": "postboot_timeout",
        })
        publish(result, evidence_path, args.beacon_host, args.beacon_port, stage="postboot_timeout", terminal=True)
        return 13

    result["postboot_rc"] = postboot_rc
    result["postboot_timed_out"] = False
    postboot_data = read_json(postboot_evidence)
    result["postboot_ready"] = bool(postboot_data.get("ready"))
    publish(result, evidence_path, args.beacon_host, args.beacon_port, stage="postboot_completed", terminal=False)
    if postboot_rc != 0 or not result["postboot_ready"]:
        result.update({
            "interactive_session_appeared": False,
            "postboot_stdout_tail": tail(postboot_stdout),
            "postboot_stderr_tail": tail(postboot_stderr),
            "ready": False,
            "status": "postboot_failed",
        })
        publish(result, evidence_path, args.beacon_host, args.beacon_port, stage="postboot_failed", terminal=True)
        return 1

    e2e_evidence.unlink(missing_ok=True)
    e2e_started_at = now_iso()
    publish(result, evidence_path, args.beacon_host, args.beacon_port, stage="e2e_started", terminal=False)
    e2e_rc, e2e_stdout, e2e_stderr, session_appeared, e2e_timed_out = run_fail_closed(
        [sys.executable, "-m", "scripts.todo_gateway_pc24x7_e2e"],
        cwd=repo_root,
        timeout_seconds=args.e2e_timeout,
    )
    if session_appeared:
        result.update({"interactive_session_appeared": True, "ready": False, "status": "interactive_session_appeared"})
        publish(result, evidence_path, args.beacon_host, args.beacon_port, stage="interactive_session_appeared", terminal=True)
        return 11
    if e2e_timed_out:
        result.update({
            "e2e_started_at": e2e_started_at,
            "e2e_rc": e2e_rc,
            "e2e_timed_out": True,
            "e2e_evidence_fresh": e2e_evidence.is_file(),
            "e2e_stdout_tail": tail(e2e_stdout),
            "e2e_stderr_tail": tail(e2e_stderr),
            "interactive_session_appeared": False,
            "ready": False,
            "status": "e2e_timeout",
        })
        publish(result, evidence_path, args.beacon_host, args.beacon_port, stage="e2e_timeout", terminal=True)
        return 14

    e2e_fresh = e2e_evidence.is_file()
    e2e_data = read_json(e2e_evidence) if e2e_fresh else {}
    result.update({
        "e2e_started_at": e2e_started_at,
        "e2e_rc": e2e_rc,
        "e2e_timed_out": False,
        "e2e_evidence_fresh": e2e_fresh,
        "e2e_ready": e2e_fresh and bool(e2e_data.get("ready")),
        "e2e_event_id": e2e_data.get("event_id"),
        "e2e_request_id": e2e_data.get("request_id"),
        "e2e_stdout_tail": tail(e2e_stdout),
        "e2e_stderr_tail": tail(e2e_stderr),
        "interactive_session_appeared": False,
    })
    publish(result, evidence_path, args.beacon_host, args.beacon_port, stage="e2e_completed", terminal=False)
    result["ready"] = e2e_rc == 0 and result["e2e_evidence_fresh"] and result["e2e_ready"]
    result["status"] = "ready" if result["ready"] else "e2e_failed"
    result["completed_at"] = now_iso()
    publish(result, evidence_path, args.beacon_host, args.beacon_port, stage=result["status"], terminal=True)
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

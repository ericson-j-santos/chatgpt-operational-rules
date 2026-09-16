#!/usr/bin/env python3
"""Restricted diagnostics for Remote Desktop Commander on Windows.

The helper intentionally avoids reading Remote Desktop Commander credential/session
contents. It reports task/action metadata, task XML execution policy, node process
count, device config metadata, launcher content, and a redacted tail of the launcher's
own log.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TASK_KEYWORDS = ("desktop commander", "desktop-commander", "desktopcommander")
ALLOWED_LAUNCHER_NAMES = {
    "start-remote-desktop-commander.cmd",
    "start_remote_desktop_commander.cmd",
}
SENSITIVE_SEGMENTS = {".ssh", ".azure", ".aws", ".gnupg", ".kube"}
SECRET_RE = re.compile(
    r"(?i)(token|secret|password|passwd|api[_-]?key|authorization)\s*[:=]\s*([^\s\"']+)"
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
TOKEN_RE = re.compile(r"\b(?:gh[pousr]_|glpat-)[A-Za-z0-9_-]{10,}")
CMD_PATH_RE = re.compile(r'(?i)(?:"([^\"]+\.cmd)"|([^\s]+\.cmd))')
MAX_LAUNCHER_BYTES = 16 * 1024
MAX_LOG_BYTES = 128 * 1024
LOG_TAIL_LINES = 40


def redact(value: str) -> str:
    value = SECRET_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", value)
    value = BEARER_RE.sub("Bearer [REDACTED]", value)
    return TOKEN_RE.sub("[REDACTED_TOKEN]", value)


def run(args: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        timeout=timeout,
        check=False,
    )


def looks_like_rdc(value: str) -> bool:
    folded = value.casefold()
    return any(keyword in folded for keyword in TASK_KEYWORDS)


def extract_launcher(action: str) -> str | None:
    for match in CMD_PATH_RE.finditer(action):
        candidate = match.group(1) or match.group(2)
        if Path(candidate).name.casefold() in ALLOWED_LAUNCHER_NAMES:
            return candidate
    return None


def xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def task_policy(task_name: str) -> dict[str, Any]:
    completed = run(["schtasks.exe", "/Query", "/TN", task_name, "/XML"])
    if completed.returncode != 0:
        return {"error": redact(completed.stderr.strip() or "task XML query failed")}
    try:
        root = ET.fromstring(completed.stdout)
    except ET.ParseError as exc:
        return {"error": f"task XML parse failed: {exc}"}
    wanted = {
        "MultipleInstancesPolicy",
        "ExecutionTimeLimit",
        "RestartOnFailure",
        "DisallowStartIfOnBatteries",
        "StopIfGoingOnBatteries",
        "StartWhenAvailable",
    }
    result: dict[str, Any] = {}
    for element in root.iter():
        name = xml_local_name(element.tag)
        if name not in wanted:
            continue
        if name == "RestartOnFailure":
            result[name] = {
                xml_local_name(child.tag): (child.text or "").strip()
                for child in list(element)
            }
        else:
            result[name] = (element.text or "").strip()
    return result


def scheduled_tasks() -> list[dict[str, Any]]:
    completed = run(["schtasks.exe", "/Query", "/FO", "CSV", "/V"])
    if completed.returncode != 0:
        return [{"error": redact(completed.stderr.strip() or "schtasks query failed")}]
    reader = csv.DictReader(io.StringIO(completed.stdout))
    matches: list[dict[str, Any]] = []
    for row in reader:
        joined = " ".join(str(v or "") for v in row.values())
        if not looks_like_rdc(joined):
            continue
        task_name = row.get("TaskName") or row.get("Nome da tarefa") or ""
        status = row.get("Status") or row.get("Estado") or ""
        action = (
            row.get("Task To Run")
            or row.get("Tarefa a ser executada")
            or row.get("Actions")
            or row.get("Ações")
            or ""
        )
        last_result = (
            row.get("Last Result")
            or row.get("Último resultado")
            or row.get("Ultimo resultado")
            or ""
        )
        last_run = (
            row.get("Last Run Time")
            or row.get("Hora da última execução")
            or row.get("Hora da ultima execução")
            or ""
        )
        matches.append(
            {
                "task_name": redact(task_name),
                "status": redact(status),
                "action": redact(action),
                "launcher": extract_launcher(action) or "",
                "last_result": redact(last_result),
                "last_run": redact(last_run),
                "policy": task_policy(task_name) if task_name else {},
            }
        )
    return matches


def node_process_count() -> dict[str, Any]:
    completed = run(["tasklist.exe", "/FI", "IMAGENAME eq node.exe", "/FO", "CSV", "/NH"])
    if completed.returncode != 0:
        return {"count": None, "error": redact(completed.stderr.strip() or "tasklist failed")}
    rows = [line for line in completed.stdout.splitlines() if line.strip() and "INFO:" not in line]
    return {"count": len(rows)}


def device_config_metadata() -> dict[str, Any]:
    path = Path.home() / ".desktop-commander-device" / "device.json"
    result: dict[str, Any] = {"path": str(path), "exists": path.is_file()}
    if not path.is_file():
        return result
    stat = path.stat()
    result.update(
        {
            "size": stat.st_size,
            "mtime_utc": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        }
    )
    return result


def launcher_log_tail() -> dict[str, Any]:
    path = Path.home() / "RemoteDesktopCommander.log"
    result: dict[str, Any] = {"path": str(path), "exists": path.is_file()}
    if not path.is_file():
        return result
    size = path.stat().st_size
    result["size"] = size
    if size > MAX_LOG_BYTES:
        with path.open("rb") as handle:
            handle.seek(max(0, size - MAX_LOG_BYTES))
            payload = handle.read(MAX_LOG_BYTES)
    else:
        payload = path.read_bytes()
    text = payload.decode("utf-8-sig", errors="replace")
    result["tail_redacted"] = redact("\n".join(text.splitlines()[-LOG_TAIL_LINES:]))
    return result


def validate_launcher_path(raw: str) -> Path:
    path = Path(raw).expanduser().resolve()
    if path.name.casefold() not in ALLOWED_LAUNCHER_NAMES:
        raise ValueError("launcher basename not authorized")
    if any(part.casefold() in SENSITIVE_SEGMENTS for part in path.parts):
        raise ValueError("launcher path contains sensitive segment")
    return path


def launcher_snapshot(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    path = validate_launcher_path(raw)
    result: dict[str, Any] = {"path": str(path), "exists": path.is_file()}
    if not path.is_file():
        return result
    payload = path.read_bytes()
    if len(payload) > MAX_LAUNCHER_BYTES:
        return {**result, "size": len(payload), "error": "launcher exceeds diagnostics size limit"}
    digest = hashlib.sha256(payload).hexdigest()
    text = payload.decode("utf-8-sig", errors="replace")
    result.update({"size": len(payload), "sha256": digest, "content_redacted": redact(text)})
    return result


def collect(explicit_launcher: str | None) -> dict[str, Any]:
    tasks = scheduled_tasks()
    discovered = [str(item.get("launcher", "")) for item in tasks if item.get("launcher")]
    launcher = explicit_launcher or (discovered[0] if len(set(discovered)) == 1 else None)
    return {
        "host": os.environ.get("COMPUTERNAME", ""),
        "tasks": tasks,
        "node_processes": node_process_count(),
        "device_config": device_config_metadata(),
        "launcher_log": launcher_log_tail(),
        "launcher": launcher_snapshot(launcher),
        "launcher_discovery_ambiguous": explicit_launcher is None and len(set(discovered)) > 1,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Restricted Remote Desktop Commander diagnostics")
    parser.add_argument("--launcher")
    ns = parser.parse_args()
    try:
        print(json.dumps(collect(ns.launcher), ensure_ascii=True, sort_keys=True))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"result": "blocked", "error": redact(str(exc))}, ensure_ascii=True, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

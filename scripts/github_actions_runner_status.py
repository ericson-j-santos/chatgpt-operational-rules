#!/usr/bin/env python3
"""Read-only diagnostics for a GitHub Actions self-hosted runner on Windows.

The script never reads runner credential/config contents. It reports only service,
process and filesystem-presence metadata needed to decide whether an existing
runner can be reactivated or a new registration is required.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

SERVICE_RE = re.compile(r"(?i)SERVICE_NAME:\s*(actions\.runner\.[^\r\n]+)")
CANDIDATE_ROOTS = [
    Path(r"C:\actions-runner"),
    Path(r"C:\dev\actions-runner"),
    Path(r"C:\ProgramData\actions-runner"),
    Path(r"C:\ProgramData\ReqSys\actions-runner"),
    Path(r"C:\ProgramData\ReqSys\GitHubActionsRunner"),
]


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


def services() -> list[dict[str, Any]]:
    queried = run(["sc.exe", "query", "type=", "service", "state=", "all"])
    names = SERVICE_RE.findall(queried.stdout if queried.returncode == 0 else "")
    result: list[dict[str, Any]] = []
    for name in sorted(set(names), key=str.casefold):
        q = run(["sc.exe", "query", name])
        qc = run(["sc.exe", "qc", name])
        state_match = re.search(r"(?im)^\s*STATE\s*:\s*\d+\s+(\w+)", q.stdout)
        start_match = re.search(r"(?im)^\s*START_TYPE\s*:\s*(\d+)\s+(\w+)", qc.stdout)
        path_match = re.search(r"(?im)^\s*BINARY_PATH_NAME\s*:\s*(.+)$", qc.stdout)
        raw_path = path_match.group(1).strip() if path_match else ""
        # Path is operational metadata, but redact command-line arguments after RunnerService.exe.
        path = re.split(r"(?i)RunnerService\.exe", raw_path, maxsplit=1)
        binary_path = (path[0] + "RunnerService.exe") if len(path) > 1 else raw_path
        result.append({
            "name": name,
            "state": state_match.group(1).upper() if state_match else "UNKNOWN",
            "start_type": start_match.group(2).upper() if start_match else "UNKNOWN",
            "binary_path": binary_path,
            "query_rc": q.returncode,
            "qc_rc": qc.returncode,
        })
    return result


def processes() -> dict[str, Any]:
    completed = run(["tasklist.exe", "/FI", "IMAGENAME eq Runner.Listener.exe", "/FO", "CSV", "/NH"])
    if completed.returncode != 0:
        return {"count": None, "returncode": completed.returncode}
    rows = []
    for line in completed.stdout.splitlines():
        if not line.strip() or line.lstrip().startswith("INFO:"):
            continue
        try:
            parsed = next(csv.reader(io.StringIO(line)))
        except (csv.Error, StopIteration):
            continue
        if parsed and parsed[0].casefold() == "runner.listener.exe":
            rows.append(parsed)
    return {"count": len(rows), "pids": [row[1] for row in rows if len(row) > 1]}


def candidate_roots(service_rows: list[dict[str, Any]]) -> list[Path]:
    roots = list(CANDIDATE_ROOTS)
    for row in service_rows:
        raw = str(row.get("binary_path") or "").strip().strip('"')
        if raw:
            p = Path(raw)
            roots.append(p.parent)
    unique: dict[str, Path] = {}
    for root in roots:
        unique[str(root).casefold()] = root
    return list(unique.values())


def roots_metadata(roots: list[Path]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for root in roots:
        exists = root.is_dir()
        row: dict[str, Any] = {
            "path": str(root),
            "exists": exists,
        }
        if exists:
            row.update({
                "runner_listener_present": (root / "bin" / "Runner.Listener.exe").is_file(),
                "runner_service_present": (root / "bin" / "RunnerService.exe").is_file(),
                "run_cmd_present": (root / "run.cmd").is_file(),
                "svc_cmd_present": (root / "svc.cmd").is_file(),
                "runner_config_present": (root / ".runner").is_file(),
                "credentials_present": (root / ".credentials").is_file(),
                "work_dir_present": (root / "_work").is_dir(),
            })
        result.append(row)
    return result


def main() -> int:
    svc = services()
    payload = {
        "host": os.environ.get("COMPUTERNAME", ""),
        "services": svc,
        "listener": processes(),
        "roots": roots_metadata(candidate_roots(svc)),
        "secret_values_exposed": False,
    }
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

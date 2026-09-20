#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
import getpass
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ACTION_ID = "prelogin.s4u.install.once"
AUTHORIZE_CONFIRM = "AUTHORIZE-ONE-TIME-PRELOGIN-ADMIN"
LAUNCH_CONFIRM = "LAUNCH-ONE-TIME-PRELOGIN-ADMIN"
EXECUTE_CONFIRM = "EXECUTE-ONE-TIME-PRELOGIN-ADMIN"
REVOKE_CONFIRM = "REVOKE-ONE-TIME-PRELOGIN-ADMIN"
MAX_WINDOW_MINUTES = 10
TASK_FOLDER = r"\Automation"
ORCH_TASK_NAME = "ReqSysOrchestrator24x7"
TASK_CREATE_OR_UPDATE = 6
TASK_LOGON_S4U = 2
TASK_RUNLEVEL_LUA = 0
TASK_TRIGGER_BOOT = 8
TASK_TRIGGER_DAILY = 2
TASK_ACTION_EXEC = 0
TASK_INSTANCES_IGNORE_NEW = 2
ACTION_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")


class PreloginAdminError(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat()


def state_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise PreloginAdminError("LOCALAPPDATA unavailable")
    return Path(local) / "ReqSys" / "CommandGateway"


def plan_path() -> Path:
    return state_dir() / "owner-prelogin-admin-once.local.json"


def result_path() -> Path:
    return state_dir() / "owner-prelogin-admin-last.json"


def audit_path() -> Path:
    return state_dir() / "owner-prelogin-admin-audit.jsonl"


def owner_fingerprint() -> str:
    raw = f"{getpass.getuser()}@{socket.gethostname()}".encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def git_capture(args: list[str]) -> str:
    cp = subprocess.run(
        ["git", "-C", str(repo_root()), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        timeout=30,
        check=False,
    )
    if cp.returncode != 0 or cp.stderr.strip():
        raise PreloginAdminError("git validation failed")
    return cp.stdout.strip()


def current_head() -> str:
    return git_capture(["rev-parse", "HEAD"])


def require_clean_tracked() -> None:
    if git_capture(["ls-files", "-m", "-d"]):
        raise PreloginAdminError("tracked worktree changes detected")


def is_windows_admin() -> bool:
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception as exc:
        raise PreloginAdminError("invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise PreloginAdminError("timestamp must contain timezone")
    return parsed.astimezone(timezone.utc)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def append_audit(payload: dict[str, Any]) -> None:
    path = audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def load_plan(action_id: str, *, require_submitted: bool = False) -> dict[str, Any]:
    path = plan_path()
    if not path.is_file():
        raise PreloginAdminError("authorization plan missing")
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PreloginAdminError("authorization plan invalid") from exc
    if plan.get("version") != 1 or plan.get("enabled") is not True:
        raise PreloginAdminError("authorization plan disabled")
    if plan.get("action_id") != action_id or action_id != ACTION_ID:
        raise PreloginAdminError("action_id mismatch")
    if str(plan.get("host", "")).casefold() != socket.gethostname().casefold():
        raise PreloginAdminError("authorization belongs to another host")
    if plan.get("owner_fingerprint") != owner_fingerprint():
        raise PreloginAdminError("owner fingerprint mismatch")
    if plan.get("consumed_at"):
        raise PreloginAdminError("authorization already consumed")
    if parse_utc(plan["expires_at"]) <= utc_now():
        raise PreloginAdminError("authorization expired")
    if plan.get("rules_head") != current_head():
        raise PreloginAdminError("rules HEAD changed")
    if require_submitted and not plan.get("submitted_at"):
        raise PreloginAdminError("elevation launch was not submitted")
    return plan


def authorize(*, host: str, minutes: int, authorization_ref: str, confirm: str) -> dict[str, Any]:
    if confirm != AUTHORIZE_CONFIRM:
        raise PreloginAdminError("authorization confirmation mismatch")
    if not ACTION_RE.fullmatch(ACTION_ID):
        raise PreloginAdminError("invalid canonical action_id")
    if not 1 <= minutes <= MAX_WINDOW_MINUTES:
        raise PreloginAdminError("authorization window must be 1..10 minutes")
    if host.casefold() != socket.gethostname().casefold():
        raise PreloginAdminError("host does not match local host")
    require_clean_tracked()
    now = utc_now()
    plan = {
        "version": 1,
        "enabled": True,
        "action_id": ACTION_ID,
        "host": socket.gethostname(),
        "owner_fingerprint": owner_fingerprint(),
        "rules_head": current_head(),
        "authorized_at": utc_iso(now),
        "expires_at": utc_iso(now + timedelta(minutes=minutes)),
        "authorization_ref_sha256": hashlib.sha256(
            authorization_ref.encode("utf-8", errors="replace")
        ).hexdigest(),
        "submitted_at": None,
        "consumed_at": None,
    }
    atomic_json(plan_path(), plan)
    append_audit({"event": "authorized", "action_id": ACTION_ID, "host": plan["host"], "timestamp": utc_iso()})
    return {k: plan[k] for k in ("action_id", "host", "expires_at", "rules_head")}


def launch(*, action_id: str, confirm: str) -> dict[str, Any]:
    if confirm != LAUNCH_CONFIRM:
        raise PreloginAdminError("launch confirmation mismatch")
    plan = load_plan(action_id)
    if plan.get("submitted_at"):
        raise PreloginAdminError("elevation launch already submitted")
    require_clean_tracked()
    plan["submitted_at"] = utc_iso()
    atomic_json(plan_path(), plan)
    script = str(Path(__file__).resolve())
    params = (
        f'"{script}" execute-elevated --action-id {ACTION_ID} '
        f'--confirm {EXECUTE_CONFIRM}'
    )
    if os.name != "nt":
        raise PreloginAdminError("UAC elevation is supported only on Windows")
    shell32 = ctypes.windll.shell32
    code = int(shell32.ShellExecuteW(None, "runas", sys.executable, params, str(repo_root()), 1))
    append_audit({
        "event": "elevation_submitted",
        "action_id": ACTION_ID,
        "host": plan["host"],
        "shell_execute_code": code,
        "timestamp": utc_iso(),
    })
    if code <= 32:
        raise PreloginAdminError(f"ShellExecuteW failed code={code}")
    return {"ok": True, "submitted": True, "shell_execute_code": code}


def _connect_task_folder():
    import win32com.client
    service = win32com.client.Dispatch("Schedule.Service")
    service.Connect()
    root = service.GetFolder("\\")
    try:
        folder = service.GetFolder(TASK_FOLDER)
    except Exception:
        folder = root.CreateFolder("Automation")
    return service, folder


def install_orchestrator_s4u() -> dict[str, Any]:
    install_root = Path(r"C:\dev\chatgpt-workers\reqsys-orchestrator-24x7-runtime")
    service_config = install_root / "service-config.json"
    supervisor = install_root / "scripts" / "service_supervisor.py"
    python_exe = Path(sys.executable)
    if not install_root.is_dir() or not service_config.is_file() or not supervisor.is_file():
        raise PreloginAdminError("orchestrator runtime incomplete")
    service, folder = _connect_task_folder()
    definition = service.NewTask(0)
    settings = definition.Settings
    settings.Enabled = True
    settings.StartWhenAvailable = True
    settings.DisallowStartIfOnBatteries = False
    settings.StopIfGoingOnBatteries = False
    settings.ExecutionTimeLimit = "PT0S"
    settings.MultipleInstances = TASK_INSTANCES_IGNORE_NEW
    settings.RestartCount = 999
    settings.RestartInterval = "PT1M"
    principal = definition.Principal
    domain = os.environ.get("USERDOMAIN", "").strip()
    user = os.environ.get("USERNAME", "").strip() or getpass.getuser()
    identity = f"{domain}\\{user}" if domain else user
    principal.UserId = identity
    principal.LogonType = TASK_LOGON_S4U
    principal.RunLevel = TASK_RUNLEVEL_LUA
    triggers = definition.Triggers
    boot = triggers.Create(TASK_TRIGGER_BOOT)
    boot.Enabled = True
    daily = triggers.Create(TASK_TRIGGER_DAILY)
    daily.Enabled = True
    daily.StartBoundary = (datetime.now().astimezone() + timedelta(seconds=30)).replace(microsecond=0).isoformat()
    daily.DaysInterval = 1
    daily.Repetition.Interval = "PT5M"
    daily.Repetition.Duration = "P1D"
    daily.Repetition.StopAtDurationEnd = False
    action = definition.Actions.Create(TASK_ACTION_EXEC)
    action.Path = str(python_exe)
    action.Arguments = f'-m scripts.service_supervisor --config "{service_config}"'
    action.WorkingDirectory = str(install_root)
    task = folder.RegisterTaskDefinition(
        ORCH_TASK_NAME,
        definition,
        TASK_CREATE_OR_UPDATE,
        identity,
        "",
        TASK_LOGON_S4U,
    )
    state = {
        "path": str(task.Path),
        "enabled": bool(task.Enabled),
        "principal_logon_type": int(task.Definition.Principal.LogonType),
        "trigger_count": int(task.Definition.Triggers.Count),
        "action_path": str(task.Definition.Actions.Item(1).Path),
    }
    if not state["enabled"] or state["principal_logon_type"] != TASK_LOGON_S4U or state["trigger_count"] != 2:
        raise PreloginAdminError("orchestrator S4U validation failed")
    return state


def execute_elevated(*, action_id: str, confirm: str) -> dict[str, Any]:
    if confirm != EXECUTE_CONFIRM:
        raise PreloginAdminError("execute confirmation mismatch")
    if not is_windows_admin():
        raise PreloginAdminError("elevated token required")
    plan = load_plan(action_id, require_submitted=True)
    require_clean_tracked()
    plan["consumed_at"] = utc_iso()
    atomic_json(plan_path(), plan)
    append_audit({"event": "consumed", "action_id": ACTION_ID, "host": plan["host"], "timestamp": plan["consumed_at"]})

    scripts_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(scripts_dir))
    import rdc_owner_arbitration as arbitration
    import rdc_task_resilience as resilience

    arbitration_result = arbitration.apply()
    rdc_result = resilience.apply(
        30,
        headless=True,
        confirm=resilience.HEADLESS_CONFIRM,
    )
    orchestrator_result = install_orchestrator_s4u()
    result = {
        "ok": True,
        "action_id": ACTION_ID,
        "host": plan["host"],
        "rules_head": plan["rules_head"],
        "rdc_arbitration": arbitration_result,
        "rdc_headless": rdc_result,
        "orchestrator_s4u": orchestrator_result,
        "completed_at": utc_iso(),
    }
    atomic_json(result_path(), result)
    append_audit({"event": "completed", "action_id": ACTION_ID, "host": plan["host"], "timestamp": result["completed_at"]})
    return result


def revoke(confirm: str) -> dict[str, Any]:
    if confirm != REVOKE_CONFIRM:
        raise PreloginAdminError("revoke confirmation mismatch")
    path = plan_path()
    existed = path.exists()
    path.unlink(missing_ok=True)
    append_audit({"event": "revoked", "action_id": ACTION_ID, "host": socket.gethostname(), "timestamp": utc_iso()})
    return {"ok": True, "revoked": existed}


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("authorize")
    pa.add_argument("--host", required=True)
    pa.add_argument("--minutes", type=int, default=10)
    pa.add_argument("--authorization-ref", required=True)
    pa.add_argument("--confirm", required=True)

    pl = sub.add_parser("launch")
    pl.add_argument("--action-id", required=True)
    pl.add_argument("--confirm", required=True)

    pe = sub.add_parser("execute-elevated")
    pe.add_argument("--action-id", required=True)
    pe.add_argument("--confirm", required=True)

    pr = sub.add_parser("revoke")
    pr.add_argument("--confirm", required=True)

    ns = parser.parse_args()
    try:
        if ns.cmd == "authorize":
            out = authorize(host=ns.host, minutes=ns.minutes, authorization_ref=ns.authorization_ref, confirm=ns.confirm)
        elif ns.cmd == "launch":
            out = launch(action_id=ns.action_id, confirm=ns.confirm)
        elif ns.cmd == "execute-elevated":
            out = execute_elevated(action_id=ns.action_id, confirm=ns.confirm)
        else:
            out = revoke(ns.confirm)
        print(json.dumps(out, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc), "error_type": type(exc).__name__}, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

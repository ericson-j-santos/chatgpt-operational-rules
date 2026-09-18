from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import win32com.client  # type: ignore
import win32cred  # type: ignore
import win32security  # type: ignore
import ntsecuritycon  # type: ignore

ACCOUNT = "ReqSysRdcSvc"
CRED_TARGET = "ReqSys/RdcSvc/TaskScheduler"
TASK_FOLDER = r"\Automation"
OLD_TASK = "RemoteDesktopCommander"
NEW_TASK = "RemoteDesktopCommanderHeadless"
PROFILE_PROBE_TASK = "ReqSysRdcSvcProfileProbe"

RUNTIME = Path(r"C:\ProgramData\ReqSys\RdcSvc")
BIN_DIR = RUNTIME / "bin"
SERVICE_NODE = BIN_DIR / "node.exe"
APP_DIR = RUNTIME / "app"
RUNNER_JS = RUNTIME / "rdc-headless-runner.cjs"
LOG_FILE = RUNTIME / "rdc-headless.log"
PROFILE_RECEIPT = RUNTIME / "service-profile.txt"
CUTOVER_RECEIPT = RUNTIME / "cutover-receipt.json"

PACKAGE = "@wonderwhy-er/desktop-commander"
VERSION = "0.2.51"
TASK_CREATE_OR_UPDATE = 6
TASK_LOGON_PASSWORD = 1
TASK_RUNLEVEL_LUA = 0
TASK_ACTION_EXEC = 0
TASK_TRIGGER_BOOT = 8
TASK_INSTANCES_IGNORE_NEW = 2


class CutoverError(RuntimeError):
    pass


def receipt(payload: dict[str, object]) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    safe = dict(payload)
    safe["secret_value_exposed"] = False
    tmp = CUTOVER_RECEIPT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(safe, ensure_ascii=True, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, CUTOVER_RECEIPT)


def run(args: list[str], timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        shell=False,
    )


def read_managed_credential() -> tuple[str, str]:
    item = win32cred.CredRead(CRED_TARGET, win32cred.CRED_TYPE_GENERIC, 0)
    user = str(item.get("UserName") or "")
    blob = item.get("CredentialBlob")
    if isinstance(blob, bytes):
        try:
            password = blob.decode("utf-16-le")
        except UnicodeDecodeError:
            password = blob.decode("utf-8")
    else:
        password = str(blob or "")
    if not user or not password:
        raise CutoverError("managed credential is incomplete")
    return user, password


def task_service():
    service = win32com.client.Dispatch("Schedule.Service")
    service.Connect()
    return service


def ensure_folder(service):
    try:
        return service.GetFolder(TASK_FOLDER)
    except Exception:
        return service.GetFolder("\\").CreateFolder(TASK_FOLDER.strip("\\"))


def get_task(folder, name: str):
    try:
        return folder.GetTask(name)
    except Exception:
        return None


def delete_task(folder, name: str) -> None:
    try:
        folder.DeleteTask(name, 0)
    except Exception:
        pass


def make_profile_probe(service, folder, user_id: str, password: str) -> Path:
    delete_task(folder, PROFILE_PROBE_TASK)
    PROFILE_RECEIPT.unlink(missing_ok=True)
    probe = RUNTIME / "service-profile-probe.vbs"
    probe.write_text(
        'Set fso = CreateObject("Scripting.FileSystemObject")\r\n'
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'Set f = fso.CreateTextFile("{PROFILE_RECEIPT}", True)\r\n'
        'f.WriteLine sh.ExpandEnvironmentStrings("%USERPROFILE%")\r\n'
        'f.Close\r\n',
        encoding="utf-8",
    )

    definition = service.NewTask(0)
    definition.RegistrationInfo.Description = "ReqSys RDC service profile discovery"
    definition.Settings.Enabled = True
    definition.Settings.AllowDemandStart = True
    definition.Settings.ExecutionTimeLimit = "PT2M"
    definition.Principal.UserId = user_id
    definition.Principal.LogonType = TASK_LOGON_PASSWORD
    definition.Principal.RunLevel = TASK_RUNLEVEL_LUA
    action = definition.Actions.Create(TASK_ACTION_EXEC)
    action.Path = r"C:\Windows\System32\cscript.exe"
    action.Arguments = f'//Nologo "{probe}"'
    action.WorkingDirectory = str(RUNTIME)
    registered = folder.RegisterTaskDefinition(
        PROFILE_PROBE_TASK,
        definition,
        TASK_CREATE_OR_UPDATE,
        user_id,
        password,
        TASK_LOGON_PASSWORD,
    )
    registered.Run("")

    deadline = time.time() + 30
    while time.time() < deadline and not PROFILE_RECEIPT.exists():
        time.sleep(0.5)
    delete_task(folder, PROFILE_PROBE_TASK)
    probe.unlink(missing_ok=True)
    if not PROFILE_RECEIPT.exists():
        raise CutoverError("service profile probe did not produce a receipt")
    profile = Path(PROFILE_RECEIPT.read_text(encoding="utf-8", errors="replace").strip())
    PROFILE_RECEIPT.unlink(missing_ok=True)
    if not profile.is_absolute():
        raise CutoverError("service profile path is invalid")
    return profile


def secure_secret_file(path: Path, service_account: str) -> None:
    svc_sid, _, _ = win32security.LookupAccountName(None, service_account)
    system_sid = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
    admin_sid = win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid, None)

    dacl = win32security.ACL()
    full = ntsecuritycon.FILE_ALL_ACCESS
    rw = ntsecuritycon.FILE_GENERIC_READ | ntsecuritycon.FILE_GENERIC_WRITE
    dacl.AddAccessAllowedAce(win32security.ACL_REVISION, rw, svc_sid)
    dacl.AddAccessAllowedAce(win32security.ACL_REVISION, full, system_sid)
    dacl.AddAccessAllowedAce(win32security.ACL_REVISION, full, admin_sid)

    sd = win32security.SECURITY_DESCRIPTOR()
    sd.SetSecurityDescriptorDacl(1, dacl, 0)
    win32security.SetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION, sd)


def provision_service_node() -> Path:
    source = shutil.which("node.exe") or shutil.which("node")
    if not source:
        raise CutoverError("node not found")
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    source_path = Path(source)
    if not SERVICE_NODE.exists() or SERVICE_NODE.stat().st_size != source_path.stat().st_size:
        shutil.copy2(source_path, SERVICE_NODE)
    completed = run([str(SERVICE_NODE), "--version"], timeout=30)
    if completed.returncode != 0:
        raise CutoverError("service-local node validation failed")
    return SERVICE_NODE


def install_pinned_package() -> Path:
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        raise CutoverError("npm not found")
    APP_DIR.mkdir(parents=True, exist_ok=True)
    completed = run(
        [
            npm,
            "install",
            "--prefix",
            str(APP_DIR),
            f"{PACKAGE}@{VERSION}",
            "--no-audit",
            "--no-fund",
            "--ignore-scripts",
        ],
        timeout=240,
    )
    if completed.returncode != 0:
        raise CutoverError("pinned package installation failed")
    package_json = APP_DIR / "node_modules" / "@wonderwhy-er" / "desktop-commander" / "package.json"
    if not package_json.is_file():
        raise CutoverError("installed package metadata missing")
    metadata = json.loads(package_json.read_text(encoding="utf-8"))
    if str(metadata.get("version")) != VERSION:
        raise CutoverError("installed package version mismatch")
    cli = APP_DIR / "node_modules" / "@wonderwhy-er" / "desktop-commander" / "dist" / "index.js"
    if not cli.is_file():
        raise CutoverError("desktop commander CLI missing")
    return cli


def write_runner(cli: Path) -> None:
    runner = f"""const fs = require('fs');
const {{ spawn }} = require('child_process');
const out = fs.openSync({json.dumps(str(LOG_FILE))}, 'a');
fs.writeSync(out, new Date().toISOString() + ' runner_start\\n');
const child = spawn(process.execPath, [{json.dumps(str(cli))}, 'remote'], {{
  stdio: ['ignore', out, out],
  windowsHide: true,
  env: process.env,
}});
child.on('exit', (code, signal) => {{
  fs.writeSync(out, new Date().toISOString() + ' child_exit code=' + code + ' signal=' + signal + '\\n');
  fs.closeSync(out);
  process.exit(code ?? 1);
}});
"""
    RUNNER_JS.write_text(runner, encoding="utf-8")


def register_headless_task(folder, user_id: str, password: str, node: str) -> None:
    definition = task_service().NewTask(0)
    definition.RegistrationInfo.Description = "ReqSys Remote Desktop Commander headless startup"
    definition.Settings.Enabled = True
    definition.Settings.AllowDemandStart = True
    definition.Settings.StartWhenAvailable = True
    definition.Settings.DisallowStartIfOnBatteries = False
    definition.Settings.StopIfGoingOnBatteries = False
    definition.Settings.ExecutionTimeLimit = "PT0S"
    definition.Settings.MultipleInstances = TASK_INSTANCES_IGNORE_NEW
    definition.Settings.RestartCount = 3
    definition.Settings.RestartInterval = "PT1M"

    definition.Principal.UserId = user_id
    definition.Principal.LogonType = TASK_LOGON_PASSWORD
    definition.Principal.RunLevel = TASK_RUNLEVEL_LUA

    trigger = definition.Triggers.Create(TASK_TRIGGER_BOOT)
    trigger.Enabled = True
    trigger.Delay = "PT20S"

    action = definition.Actions.Create(TASK_ACTION_EXEC)
    action.Path = node
    action.Arguments = f'"{RUNNER_JS}"'
    action.WorkingDirectory = str(RUNTIME)

    folder.RegisterTaskDefinition(
        NEW_TASK,
        definition,
        TASK_CREATE_OR_UPDATE,
        user_id,
        password,
        TASK_LOGON_PASSWORD,
    )


def wait_ready(folder) -> dict[str, object]:
    LOG_FILE.unlink(missing_ok=True)
    task = folder.GetTask(NEW_TASK)
    task.Run("")
    deadline = time.time() + 75
    saw_ready = False
    saw_restored = False
    while time.time() < deadline:
        if LOG_FILE.exists():
            text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
            saw_ready = "Device ready:" in text
            saw_restored = "Session restored" in text
            if saw_ready and saw_restored:
                break
        time.sleep(1.0)
    return {
        "task_state": int(task.State),
        "last_task_result": int(task.LastTaskResult),
        "log_session_restored": saw_restored,
        "log_device_ready": saw_ready,
    }


def main() -> int:
    stage = "preflight"
    copied_secret = False
    new_task_created = False
    old_task_was_enabled = None
    try:
        computer = (os.environ.get("COMPUTERNAME", "").strip() or socket.gethostname().strip())
        user_id, password = read_managed_credential()
        if not user_id.casefold().endswith("\\" + ACCOUNT.casefold()):
            raise CutoverError("managed credential account mismatch")

        service = task_service()
        folder = ensure_folder(service)
        old_task = get_task(folder, OLD_TASK)
        old_task_was_enabled = bool(old_task.Enabled) if old_task is not None else None

        stage = "discover_service_profile"
        profile = make_profile_probe(service, folder, user_id, password)

        stage = "locate_source_session"
        current_profile = Path(os.environ.get("USERPROFILE", ""))
        source = current_profile / ".desktop-commander-device" / "device.json"
        if not source.is_file():
            raise CutoverError("source persisted RDC session missing")

        stage = "copy_session"
        target_dir = profile / ".desktop-commander-device"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "device.json"
        backup = target.with_suffix(".json.pre-cutover")
        if target.exists():
            shutil.copy2(target, backup)
        shutil.copy2(source, target)
        secure_secret_file(target, ACCOUNT)
        copied_secret = True

        stage = "install_pinned_package"
        cli = install_pinned_package()
        node = provision_service_node()
        write_runner(cli)

        stage = "register_headless_task"
        register_headless_task(folder, user_id, password, node)
        new_task_created = True

        stage = "manual_headless_e2e"
        evidence = wait_ready(folder)
        if not evidence["log_session_restored"] or not evidence["log_device_ready"]:
            raise CutoverError("headless task did not restore the session and become ready")

        stage = "disable_legacy_autostart"
        if old_task is not None:
            old_task.Enabled = False

        payload = {
            "result": "ready",
            "stage": "complete",
            "host": computer,
            "package_version": VERSION,
            "service_node_under_programdata": str(node).casefold().startswith(str(RUNTIME).casefold()),
            "service_profile_detected": True,
            "session_copied": True,
            "session_file_acl_restricted": True,
            "headless_task_created": True,
            "headless_trigger": "Boot",
            "headless_logon_type": "Password",
            "legacy_task_present": old_task is not None,
            "legacy_task_disabled": bool(old_task is not None),
            **evidence,
        }
        receipt(payload)
        print(json.dumps({**payload, "secret_value_exposed": False}, ensure_ascii=True, sort_keys=True))
        return 0
    except Exception as exc:
        try:
            service = task_service()
            folder = ensure_folder(service)
            if new_task_created:
                delete_task(folder, NEW_TASK)
            old_task = get_task(folder, OLD_TASK)
            if old_task is not None and old_task_was_enabled is not None:
                old_task.Enabled = old_task_was_enabled
        except Exception:
            pass
        if copied_secret:
            try:
                profile = locals().get("profile")
                if profile:
                    target = Path(profile) / ".desktop-commander-device" / "device.json"
                    target.unlink(missing_ok=True)
            except Exception:
                pass
        payload = {
            "result": "blocked",
            "stage": stage,
            "error_type": type(exc).__name__,
            "rollback_new_task_attempted": new_task_created,
            "rollback_legacy_state_attempted": old_task_was_enabled is not None,
            "rollback_session_copy_attempted": copied_secret,
        }
        receipt(payload)
        print(json.dumps({**payload, "secret_value_exposed": False}, ensure_ascii=True, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

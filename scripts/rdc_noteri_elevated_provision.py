from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import string
import subprocess
import sys
import time
from pathlib import Path

import ntsecuritycon  # type: ignore
import win32com.client  # type: ignore
import win32cred  # type: ignore
import win32net  # type: ignore
import win32netcon  # type: ignore
import win32security  # type: ignore

ACCOUNT = "ReqSysRdcSvc"
CRED_TARGET = "ReqSys/RdcSvc/TaskScheduler"
TASK_FOLDER = r"\Automation"
HEADLESS_TASK = "RemoteDesktopCommanderHeadless"
PROFILE_TASK = "ReqSysRdcSvcProfileProbe"
TOOLCHAIN_TASK = "ReqSysRdcSvcToolchainProbe"

RUNTIME = Path(r"C:\ProgramData\ReqSys\RdcSvc")
BIN_DIR = RUNTIME / "bin"
SERVICE_NODE = BIN_DIR / "node.exe"
PYTHON_DIR = RUNTIME / "python"
SERVICE_PYTHON = PYTHON_DIR / "python.exe"
APP_DIR = RUNTIME / "app"
RUNNER_JS = RUNTIME / "rdc-headless-runner.cjs"
LOG_FILE = RUNTIME / "rdc-headless.log"
PROFILE_RECEIPT = RUNTIME / "service-profile.txt"
TOOLCHAIN_PROBE = RUNTIME / "toolchain-probe.py"
TOOLCHAIN_RECEIPT = RUNTIME / "toolchain-probe.json"
RECEIPT = RUNTIME / "noteri-provision-receipt.json"

PACKAGE = "@wonderwhy-er/desktop-commander"
VERSION = "0.2.51"

TASK_CREATE_OR_UPDATE = 6
TASK_LOGON_PASSWORD = 1
TASK_RUNLEVEL_LUA = 0
TASK_ACTION_EXEC = 0
TASK_TRIGGER_BOOT = 8
TASK_INSTANCES_IGNORE_NEW = 2


class ProvisionError(RuntimeError):
    pass


def write_receipt(payload: dict[str, object]) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    safe = dict(payload)
    safe["secret_value_exposed"] = False
    temp = RECEIPT.with_suffix(".json.tmp")
    temp.write_text(json.dumps(safe, ensure_ascii=True, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, RECEIPT)


def run(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess[str]:
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


def random_password(length: int = 48) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#%^*_-+="
    for _ in range(50):
        value = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in value)
            and any(c.isupper() for c in value)
            and any(c.isdigit() for c in value)
            and any(c in "!@#%^*_-+=" for c in value)
        ):
            return value
    raise ProvisionError("strong password generation failed")


def account_exists() -> bool:
    try:
        win32net.NetUserGetInfo(None, ACCOUNT, 1)
        return True
    except Exception as exc:
        if getattr(exc, "winerror", None) == 2221:
            return False
        raise


def create_account(password: str) -> None:
    info = {
        "name": ACCOUNT,
        "password": password,
        "password_age": 0,
        "priv": win32netcon.USER_PRIV_USER,
        "home_dir": None,
        "comment": "ReqSys Remote Desktop Commander headless account",
        "flags": (
            win32netcon.UF_SCRIPT
            | win32netcon.UF_NORMAL_ACCOUNT
            | win32netcon.UF_DONT_EXPIRE_PASSWD
            | win32netcon.UF_PASSWD_CANT_CHANGE
        ),
        "script_path": None,
    }
    win32net.NetUserAdd(None, 1, info)


def configure_rights() -> None:
    sid, _, _ = win32security.LookupAccountName(None, ACCOUNT)
    policy = win32security.LsaOpenPolicy(None, win32security.POLICY_ALL_ACCESS)
    win32security.LsaAddAccountRights(
        policy,
        sid,
        (
            "SeBatchLogonRight",
            "SeDenyInteractiveLogonRight",
            "SeDenyRemoteInteractiveLogonRight",
        ),
    )


def write_credential(user_id: str, password: str) -> None:
    win32cred.CredWrite(
        {
            "Type": win32cred.CRED_TYPE_GENERIC,
            "TargetName": CRED_TARGET,
            "UserName": user_id,
            "CredentialBlob": password,
            "Comment": "ReqSys RDC Task Scheduler credential",
            "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
        },
        0,
    )


def read_credential() -> tuple[str, str] | None:
    try:
        item = win32cred.CredRead(CRED_TARGET, win32cred.CRED_TYPE_GENERIC, 0)
    except Exception as exc:
        if getattr(exc, "winerror", None) == 1168:
            return None
        raise
    blob = item.get("CredentialBlob")
    if isinstance(blob, bytes):
        try:
            password = blob.decode("utf-16-le")
        except UnicodeDecodeError:
            password = blob.decode("utf-8")
    else:
        password = str(blob or "")
    return str(item.get("UserName") or ""), password


def task_service():
    service = win32com.client.Dispatch("Schedule.Service")
    service.Connect()
    return service


def ensure_folder(service):
    try:
        return service.GetFolder(TASK_FOLDER)
    except Exception:
        return service.GetFolder("\\").CreateFolder(TASK_FOLDER.strip("\\"))


def delete_task(folder, name: str) -> None:
    try:
        folder.DeleteTask(name, 0)
    except Exception:
        pass


def grant_runtime_acl(user_id: str) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    completed = run(
        [
            r"C:\Windows\System32\icacls.exe",
            str(RUNTIME),
            "/grant",
            f"{user_id}:(OI)(CI)M",
            "/T",
            "/C",
        ],
        timeout=120,
    )
    if completed.returncode not in (0,):
        raise ProvisionError("runtime ACL grant failed")


def grant_source_session_read(user_id: str, source_dir: Path, source_file: Path) -> None:
    for path, grant in ((source_dir, f"{user_id}:(RX)"), (source_file, f"{user_id}:(R)")):
        completed = run(
            [r"C:\Windows\System32\icacls.exe", str(path), "/grant", grant, "/C"],
            timeout=60,
        )
        if completed.returncode != 0:
            raise ProvisionError("source session ACL grant failed")


def discover_profile(folder, user_id: str, password: str) -> Path:
    delete_task(folder, PROFILE_TASK)
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

    definition = task_service().NewTask(0)
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
    task = folder.RegisterTaskDefinition(
        PROFILE_TASK,
        definition,
        TASK_CREATE_OR_UPDATE,
        user_id,
        password,
        TASK_LOGON_PASSWORD,
    )
    task.Run("")
    deadline = time.time() + 30
    while time.time() < deadline and not PROFILE_RECEIPT.exists():
        time.sleep(0.5)
    last_result = int(task.LastTaskResult)
    delete_task(folder, PROFILE_TASK)
    probe.unlink(missing_ok=True)
    if not PROFILE_RECEIPT.exists() or last_result != 0:
        raise ProvisionError("service profile probe failed")
    profile = Path(PROFILE_RECEIPT.read_text(encoding="utf-8", errors="replace").strip())
    PROFILE_RECEIPT.unlink(missing_ok=True)
    if not profile.is_absolute():
        raise ProvisionError("invalid service profile")
    return profile


def provision_python(source_base: Path) -> None:
    if not source_base.is_dir():
        raise ProvisionError("source Python base missing")
    shutil.copytree(source_base, PYTHON_DIR, dirs_exist_ok=True)
    check = run([str(SERVICE_PYTHON), "--version"], timeout=30)
    if check.returncode != 0:
        raise ProvisionError("service Python validation failed")


def provision_node() -> tuple[Path, Path]:
    node = shutil.which("node.exe") or shutil.which("node")
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not node or not npm:
        raise ProvisionError("node/npm not found")
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(node, SERVICE_NODE)
    check = run([str(SERVICE_NODE), "--version"], timeout=30)
    if check.returncode != 0:
        raise ProvisionError("service Node validation failed")
    return Path(node), Path(npm)


def install_package(npm: Path) -> Path:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    completed = run(
        [
            str(npm),
            "install",
            "--prefix",
            str(APP_DIR),
            f"{PACKAGE}@{VERSION}",
            "--no-audit",
            "--no-fund",
            "--ignore-scripts",
        ],
        timeout=300,
    )
    if completed.returncode != 0:
        raise ProvisionError("Desktop Commander package installation failed")
    package_json = APP_DIR / "node_modules" / "@wonderwhy-er" / "desktop-commander" / "package.json"
    cli = APP_DIR / "node_modules" / "@wonderwhy-er" / "desktop-commander" / "dist" / "index.js"
    if not package_json.is_file() or not cli.is_file():
        raise ProvisionError("Desktop Commander package files missing")
    metadata = json.loads(package_json.read_text(encoding="utf-8"))
    if str(metadata.get("version")) != VERSION:
        raise ProvisionError("Desktop Commander version mismatch")
    return cli


def copy_initial_session(source: Path, service_profile: Path) -> Path:
    target_dir = service_profile / ".desktop-commander-device"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "device.json"
    shutil.copy2(source, target)
    return target


def write_runner(source_session: Path, target_session: Path, cli: Path) -> None:
    python_scripts = PYTHON_DIR / "Scripts"
    path_prefix = ";".join(
        [
            str(PYTHON_DIR),
            str(python_scripts),
            str(BIN_DIR),
            r"C:\Program Files\Git\cmd",
        ]
    )
    runner = f"""const fs = require('fs');
const path = require('path');
const {{ spawn }} = require('child_process');
const SOURCE = {json.dumps(str(source_session))};
const TARGET = {json.dumps(str(target_session))};
const LOG = {json.dumps(str(LOG_FILE))};
fs.mkdirSync(path.dirname(TARGET), {{ recursive: true }});
fs.copyFileSync(SOURCE, TARGET);
process.env.PATH = {json.dumps(path_prefix)} + ';' + (process.env.PATH || '');
const out = fs.openSync(LOG, 'a');
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


def register_headless(folder, user_id: str, password: str) -> None:
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
    action.Path = str(SERVICE_NODE)
    action.Arguments = f'"{RUNNER_JS}"'
    action.WorkingDirectory = str(RUNTIME)

    folder.RegisterTaskDefinition(
        HEADLESS_TASK,
        definition,
        TASK_CREATE_OR_UPDATE,
        user_id,
        password,
        TASK_LOGON_PASSWORD,
    )


def validate_toolchain(folder, user_id: str, password: str, source_session: Path) -> dict[str, object]:
    delete_task(folder, TOOLCHAIN_TASK)
    TOOLCHAIN_RECEIPT.unlink(missing_ok=True)
    TOOLCHAIN_PROBE.write_text(
        "from __future__ import annotations\n"
        "import json, subprocess, sys\n"
        "from pathlib import Path\n"
        f"source=Path({str(source_session)!r})\n"
        f"node=Path({str(SERVICE_NODE)!r})\n"
        f"out=Path({str(TOOLCHAIN_RECEIPT)!r})\n"
        "payload={'python_ok': sys.version_info[:2] >= (3, 12), "
        "'node_present': node.is_file(), 'source_session_readable': False, "
        "'secret_value_exposed': False}\n"
        "try:\n"
        "    payload['source_session_readable'] = source.is_file() and len(source.read_bytes()) > 0\n"
        "except Exception:\n"
        "    payload['source_session_readable'] = False\n"
        "try:\n"
        "    cp=subprocess.run([str(node),'--version'],capture_output=True,text=True,timeout=15,check=False)\n"
        "    payload['node_ok'] = cp.returncode == 0\n"
        "except Exception:\n"
        "    payload['node_ok'] = False\n"
        "out.write_text(json.dumps(payload,sort_keys=True),encoding='utf-8')\n",
        encoding="utf-8",
    )

    definition = task_service().NewTask(0)
    definition.RegistrationInfo.Description = "ReqSys RDC service toolchain validation"
    definition.Settings.Enabled = True
    definition.Settings.AllowDemandStart = True
    definition.Settings.ExecutionTimeLimit = "PT2M"
    definition.Principal.UserId = user_id
    definition.Principal.LogonType = TASK_LOGON_PASSWORD
    definition.Principal.RunLevel = TASK_RUNLEVEL_LUA
    action = definition.Actions.Create(TASK_ACTION_EXEC)
    action.Path = str(SERVICE_PYTHON)
    action.Arguments = f'"{TOOLCHAIN_PROBE}"'
    action.WorkingDirectory = str(RUNTIME)

    task = folder.RegisterTaskDefinition(
        TOOLCHAIN_TASK,
        definition,
        TASK_CREATE_OR_UPDATE,
        user_id,
        password,
        TASK_LOGON_PASSWORD,
    )
    task.Run("")
    deadline = time.time() + 45
    while time.time() < deadline and not TOOLCHAIN_RECEIPT.exists():
        time.sleep(0.5)
    last_result = int(task.LastTaskResult)
    delete_task(folder, TOOLCHAIN_TASK)
    if not TOOLCHAIN_RECEIPT.exists():
        raise ProvisionError("toolchain probe receipt missing")
    payload = json.loads(TOOLCHAIN_RECEIPT.read_text(encoding="utf-8"))
    TOOLCHAIN_RECEIPT.unlink(missing_ok=True)
    TOOLCHAIN_PROBE.unlink(missing_ok=True)
    payload["last_task_result"] = last_result
    if not (
        last_result == 0
        and payload.get("python_ok") is True
        and payload.get("node_ok") is True
        and payload.get("source_session_readable") is True
    ):
        raise ProvisionError("service toolchain probe failed")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--correlation-id", required=True)
    args = parser.parse_args()

    stage = "preflight"
    created_account = False
    try:
        if not bool(__import__("ctypes").windll.shell32.IsUserAnAdmin()):
            raise ProvisionError("elevated administrator token required")

        host = socket.gethostname()
        user_id = f"{host}\\{ACCOUNT}"
        source_profile = Path(os.environ.get("USERPROFILE", ""))
        source_session_dir = source_profile / ".desktop-commander-device"
        source_session = source_session_dir / "device.json"
        if not source_session.is_file():
            raise ProvisionError("source RDC session missing")

        stage = "account"
        stored = read_credential()
        if not account_exists():
            password = random_password()
            create_account(password)
            created_account = True
            write_credential(user_id, password)
        else:
            if stored is None:
                raise ProvisionError("existing service account has no managed credential")
            stored_user, password = stored
            if stored_user.casefold() != user_id.casefold() or not password:
                raise ProvisionError("managed credential metadata mismatch")
        if created_account:
            stored = (user_id, password)

        stage = "rights"
        configure_rights()
        grant_runtime_acl(user_id)
        grant_source_session_read(user_id, source_session_dir, source_session)

        service = task_service()
        folder = ensure_folder(service)

        stage = "profile"
        service_profile = discover_profile(folder, user_id, password)

        stage = "python"
        provision_python(Path(sys.base_prefix))

        stage = "node"
        _node_source, npm = provision_node()

        stage = "package"
        cli = install_package(npm)

        stage = "session"
        target_session = copy_initial_session(source_session, service_profile)

        stage = "runner"
        write_runner(source_session, target_session, cli)

        stage = "task"
        register_headless(folder, user_id, password)

        stage = "toolchain_probe"
        probe = validate_toolchain(folder, user_id, password, source_session)

        task = folder.GetTask(HEADLESS_TASK)
        payload = {
            "result": "ready",
            "stage": "complete",
            "correlation_id": args.correlation_id,
            "account": ACCOUNT,
            "account_created": created_account,
            "headless_task_present": True,
            "headless_task_enabled": bool(task.Enabled),
            "headless_task_state": int(task.State),
            "headless_task_last_result": int(task.LastTaskResult),
            "headless_trigger_boot": any(int(t.Type) == TASK_TRIGGER_BOOT for t in task.Definition.Triggers),
            "headless_logon_type_password": int(task.Definition.Principal.LogonType) == TASK_LOGON_PASSWORD,
            "package_version": VERSION,
            "service_python_present": SERVICE_PYTHON.is_file(),
            "service_node_present": SERVICE_NODE.is_file(),
            "source_session_readable_by_probe": bool(probe.get("source_session_readable")),
            "toolchain_probe_last_result": probe.get("last_task_result"),
            "rdc_headless_started_during_provision": False,
            "legacy_controller_changed": False,
        }
        write_receipt(payload)
        return 0
    except Exception as exc:
        payload = {
            "result": "blocked",
            "stage": stage,
            "correlation_id": args.correlation_id,
            "error_type": type(exc).__name__,
            "created_account": created_account,
        }
        write_receipt(payload)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

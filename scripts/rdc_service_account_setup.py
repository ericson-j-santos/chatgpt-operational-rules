from __future__ import annotations

import json
import os
import secrets
import socket
import string
import time
from pathlib import Path

import win32com.client  # type: ignore
import win32cred  # type: ignore
import win32net  # type: ignore
import win32netcon  # type: ignore
import win32security  # type: ignore
import ntsecuritycon  # type: ignore

ACCOUNT = "ReqSysRdcSvc"
CRED_TARGET = "ReqSys/RdcSvc/TaskScheduler"
TASK_FOLDER = r"\Automation"
TEST_TASK = "ReqSysRdcSvcCredentialPreflight"
RUNTIME_DIR = Path(r"C:\ProgramData\ReqSys\RdcSvc")
IDENTITY_FILE = RUNTIME_DIR / "identity.txt"
VBS_FILE = RUNTIME_DIR / "identity_probe.vbs"
RECEIPT_FILE = RUNTIME_DIR / "setup-receipt.json"

TASK_CREATE_OR_UPDATE = 6
TASK_LOGON_PASSWORD = 1
TASK_RUNLEVEL_LUA = 0
TASK_ACTION_EXEC = 0
TASK_INSTANCES_IGNORE_NEW = 2


class SetupError(RuntimeError):
    pass


def write_receipt(payload: dict[str, object]) -> None:
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        safe = dict(payload)
        safe["secret_value_exposed"] = False
        temp = RECEIPT_FILE.with_suffix(".json.tmp")
        temp.write_text(json.dumps(safe, ensure_ascii=True, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp, RECEIPT_FILE)
    except Exception:
        pass


def random_password(length: int = 40) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#%^*_-+="
    for _ in range(20):
        value = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in value)
            and any(c.isupper() for c in value)
            and any(c.isdigit() for c in value)
            and any(c in "!@#%^*_-+=" for c in value)
        ):
            return value
    raise SetupError("failed to generate strong password")


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
        "comment": "ReqSys Remote Desktop Commander non-interactive service account",
        "flags": (
            win32netcon.UF_SCRIPT
            | win32netcon.UF_NORMAL_ACCOUNT
            | win32netcon.UF_DONT_EXPIRE_PASSWD
            | win32netcon.UF_PASSWD_CANT_CHANGE
        ),
        "script_path": None,
    }
    win32net.NetUserAdd(None, 1, info)


def get_sid():
    sid, _, _ = win32security.LookupAccountName(None, ACCOUNT)
    return sid


def configure_logon_rights() -> None:
    if not all(hasattr(win32security, name) for name in ("LsaOpenPolicy", "LsaAddAccountRights")):
        raise SetupError("required LSA APIs unavailable")
    policy = win32security.LsaOpenPolicy(None, win32security.POLICY_ALL_ACCESS)
    sid = get_sid()
    rights = (
        "SeBatchLogonRight",
        "SeDenyInteractiveLogonRight",
        "SeDenyRemoteInteractiveLogonRight",
    )
    win32security.LsaAddAccountRights(policy, sid, rights)


def grant_runtime_acl() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    sid = get_sid()
    sd = win32security.GetFileSecurity(str(RUNTIME_DIR), win32security.DACL_SECURITY_INFORMATION)
    dacl = sd.GetSecurityDescriptorDacl()
    if dacl is None:
        dacl = win32security.ACL()
    flags = win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE
    mask = (
        ntsecuritycon.FILE_GENERIC_READ
        | ntsecuritycon.FILE_GENERIC_WRITE
        | ntsecuritycon.FILE_GENERIC_EXECUTE
    )
    dacl.AddAccessAllowedAceEx(win32security.ACL_REVISION_DS, flags, mask, sid)
    sd.SetSecurityDescriptorDacl(1, dacl, 0)
    win32security.SetFileSecurity(str(RUNTIME_DIR), win32security.DACL_SECURITY_INFORMATION, sd)


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
        if getattr(exc, "winerror", None) in (1168,):
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


def delete_credential() -> None:
    try:
        win32cred.CredDelete(CRED_TARGET, win32cred.CRED_TYPE_GENERIC, 0)
    except Exception as exc:
        if getattr(exc, "winerror", None) not in (1168,):
            raise


def task_service():
    service = win32com.client.Dispatch("Schedule.Service")
    service.Connect()
    return service


def ensure_task_folder(service):
    try:
        return service.GetFolder(TASK_FOLDER)
    except Exception:
        root = service.GetFolder("\\")
        return root.CreateFolder(TASK_FOLDER.strip("\\"))


def delete_test_task(folder) -> None:
    try:
        folder.DeleteTask(TEST_TASK, 0)
    except Exception:
        pass


def write_probe_script() -> None:
    content = (
        'Set fso = CreateObject("Scripting.FileSystemObject")\r\n'
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'Set f = fso.CreateTextFile("{str(IDENTITY_FILE)}", True)\r\n'
        'f.WriteLine sh.ExpandEnvironmentStrings("%USERNAME%")\r\n'
        'f.Close\r\n'
    )
    VBS_FILE.write_text(content, encoding="utf-8")


def register_and_run_test(user_id: str, password: str) -> dict[str, object]:
    service = task_service()
    folder = ensure_task_folder(service)
    delete_test_task(folder)
    if IDENTITY_FILE.exists():
        IDENTITY_FILE.unlink()

    definition = service.NewTask(0)
    definition.RegistrationInfo.Description = "ReqSys RDC non-interactive credential validation"
    definition.Settings.Enabled = True
    definition.Settings.AllowDemandStart = True
    definition.Settings.StartWhenAvailable = True
    definition.Settings.DisallowStartIfOnBatteries = False
    definition.Settings.StopIfGoingOnBatteries = False
    definition.Settings.ExecutionTimeLimit = "PT2M"
    definition.Settings.MultipleInstances = TASK_INSTANCES_IGNORE_NEW

    definition.Principal.UserId = user_id
    definition.Principal.LogonType = TASK_LOGON_PASSWORD
    definition.Principal.RunLevel = TASK_RUNLEVEL_LUA

    action = definition.Actions.Create(TASK_ACTION_EXEC)
    action.Path = r"C:\Windows\System32\cscript.exe"
    action.Arguments = f'//Nologo "{VBS_FILE}"'
    action.WorkingDirectory = str(RUNTIME_DIR)

    registered = folder.RegisterTaskDefinition(
        TEST_TASK,
        definition,
        TASK_CREATE_OR_UPDATE,
        user_id,
        password,
        TASK_LOGON_PASSWORD,
    )
    registered.Run("")

    deadline = time.time() + 30
    while time.time() < deadline and not IDENTITY_FILE.exists():
        time.sleep(0.5)

    if not IDENTITY_FILE.exists():
        last_result = int(registered.LastTaskResult)
        delete_test_task(folder)
        raise SetupError(f"test task produced no identity receipt; last_result={last_result}")

    observed = IDENTITY_FILE.read_text(encoding="utf-8", errors="replace").strip()
    last_result = int(registered.LastTaskResult)
    delete_test_task(folder)
    IDENTITY_FILE.unlink(missing_ok=True)
    VBS_FILE.unlink(missing_ok=True)

    if observed.casefold() != ACCOUNT.casefold():
        raise SetupError("test task ran under unexpected identity")
    if last_result != 0:
        raise SetupError(f"test task last result was {last_result}")

    return {
        "identity_validated": True,
        "last_task_result": last_result,
        "test_task_removed": True,
    }


def main() -> int:
    created_user = False
    created_credential = False
    stage = "resolve_host"
    try:
        computer = (os.environ.get("COMPUTERNAME", "").strip() or socket.gethostname().strip())
        if not computer:
            raise SetupError("host name unavailable")
        user_id = f"{computer}\\{ACCOUNT}"

        stage = "inspect_existing"
        exists = account_exists()
        stored = read_credential()

        if exists and stored is None:
            write_receipt({
                "result": "blocked",
                "stage": stage,
                "reason": "existing_account_without_managed_credential",
                "account": ACCOUNT,
            })
            return 3

        if not exists:
            if stored is not None:
                delete_credential()
            stage = "create_account"
            password = random_password()
            create_account(password)
            created_user = True

            stage = "configure_logon_rights"
            configure_logon_rights()

            stage = "grant_runtime_acl"
            grant_runtime_acl()

            stage = "store_credential"
            write_credential(user_id, password)
            created_credential = True
        else:
            stored_user, password = stored
            if stored_user.casefold() != user_id.casefold() or not password:
                raise SetupError("managed credential metadata mismatch")
            stage = "configure_logon_rights"
            configure_logon_rights()
            stage = "grant_runtime_acl"
            grant_runtime_acl()

        stage = "write_probe"
        write_probe_script()
        stage = "register_and_run_test"
        evidence = register_and_run_test(user_id, password)

        payload = {
            "result": "ready",
            "stage": "complete",
            "account": ACCOUNT,
            "account_created": created_user,
            "credential_managed": True,
            "credential_target": CRED_TARGET,
            "batch_logon_configured": True,
            "interactive_logon_denied": True,
            "remote_interactive_logon_denied": True,
            **evidence,
        }
        write_receipt(payload)
        print(json.dumps({**payload, "secret_value_exposed": False}, sort_keys=True))
        return 0
    except Exception as exc:
        winerror = getattr(exc, "winerror", None)
        if created_credential:
            try:
                delete_credential()
            except Exception:
                pass
        if created_user:
            try:
                win32net.NetUserDel(None, ACCOUNT)
            except Exception:
                pass
        try:
            service = task_service()
            folder = ensure_task_folder(service)
            delete_test_task(folder)
        except Exception:
            pass
        try:
            IDENTITY_FILE.unlink(missing_ok=True)
            VBS_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        payload = {
            "result": "blocked",
            "stage": stage,
            "error_type": type(exc).__name__,
            "winerror": winerror,
            "rollback_user_attempted": created_user,
            "rollback_credential_attempted": created_credential,
        }
        write_receipt(payload)
        print(json.dumps({**payload, "secret_value_exposed": False}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

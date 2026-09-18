from __future__ import annotations

import json
from pathlib import Path

import win32com.client  # type: ignore
import win32cred  # type: ignore
import win32net  # type: ignore
import win32netcon  # type: ignore
import win32security  # type: ignore

ACCOUNT = "ReqSysRdcSvc"
CRED_TARGET = "ReqSys/RdcSvc/TaskScheduler"
TASK_FOLDER = r"\Automation"
TEST_TASK = "ReqSysRdcSvcCredentialPreflight"
RECEIPT_FILE = Path(r"C:\ProgramData\ReqSys\RdcSvc\setup-receipt.json")


def credential_metadata() -> dict[str, object]:
    try:
        item = win32cred.CredRead(CRED_TARGET, win32cred.CRED_TYPE_GENERIC, 0)
        return {
            "credential_present": True,
            "credential_target": item.get("TargetName") == CRED_TARGET,
            "credential_user_present": bool(item.get("UserName")),
            "credential_blob_present": bool(item.get("CredentialBlob")),
            "secret_value_exposed": False,
        }
    except Exception as exc:
        return {
            "credential_present": False,
            "credential_error_type": type(exc).__name__,
            "secret_value_exposed": False,
        }


def main() -> int:
    result: dict[str, object] = {
        "account": ACCOUNT,
        "account_exists": False,
        "is_administrator": False,
        "batch_logon": False,
        "interactive_logon_denied": False,
        "remote_interactive_logon_denied": False,
        "test_task_present": False,
        "receipt_result": None,
        "receipt_stage": None,
        "secret_value_exposed": False,
    }

    try:
        win32net.NetUserGetInfo(None, ACCOUNT, 1)
        result["account_exists"] = True
        groups = [str(x).casefold() for x in win32net.NetUserGetLocalGroups(None, ACCOUNT, 0, win32netcon.LG_INCLUDE_INDIRECT)]
        result["is_administrator"] = any(x in {"administrators", "administradores"} for x in groups)

        sid, _, _ = win32security.LookupAccountName(None, ACCOUNT)
        policy = win32security.LsaOpenPolicy(None, win32security.POLICY_LOOKUP_NAMES)
        try:
            rights = {str(x) for x in win32security.LsaEnumerateAccountRights(policy, sid)}
        except Exception:
            rights = set()
        result["batch_logon"] = "SeBatchLogonRight" in rights
        result["interactive_logon_denied"] = "SeDenyInteractiveLogonRight" in rights
        result["remote_interactive_logon_denied"] = "SeDenyRemoteInteractiveLogonRight" in rights

        result.update(credential_metadata())

        service = win32com.client.Dispatch("Schedule.Service")
        service.Connect()
        folder = service.GetFolder(TASK_FOLDER)
        try:
            folder.GetTask(TEST_TASK)
            result["test_task_present"] = True
        except Exception:
            result["test_task_present"] = False

        if RECEIPT_FILE.is_file():
            receipt = json.loads(RECEIPT_FILE.read_text(encoding="utf-8"))
            result["receipt_result"] = receipt.get("result")
            result["receipt_stage"] = receipt.get("stage")
            result["receipt_identity_validated"] = bool(receipt.get("identity_validated"))
            result["receipt_last_task_result"] = receipt.get("last_task_result")
            result["receipt_secret_value_exposed"] = bool(receipt.get("secret_value_exposed"))
    except Exception as exc:
        result["status_error_type"] = type(exc).__name__

    ok = (
        result.get("account_exists") is True
        and result.get("is_administrator") is False
        and result.get("batch_logon") is True
        and result.get("interactive_logon_denied") is True
        and result.get("remote_interactive_logon_denied") is True
        and result.get("credential_present") is True
        and result.get("credential_blob_present") is True
        and result.get("test_task_present") is False
        and result.get("receipt_result") == "ready"
        and result.get("receipt_stage") == "complete"
        and result.get("receipt_identity_validated") is True
        and result.get("receipt_last_task_result") == 0
        and result.get("receipt_secret_value_exposed") is False
    )
    result["ready"] = ok
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import json
import os
import py_compile
import shutil
from pathlib import Path

RUNTIME = Path(os.environ["LOCALAPPDATA"]) / "ReqSys" / "TodoGlobal24x7"
POSTBOOT = RUNTIME / "postboot_runner.py"
BACKUP = RUNTIME / "postboot_runner.before-relay.bak.py"
TARGET = Path(r"C:\dev\chatgpt-workers\bin\gitlab_relay_registrar.py")


def patch_postboot(text: str) -> str:
    if "start_relay_registrar" in text:
        return text
    anchor = 'VALIDATOR = BASE / "postboot_validate.py"\n'
    insert = anchor + (
        'REGISTRAR = Path(r"C:\\\\dev\\\\chatgpt-workers\\\\bin\\\\gitlab_relay_registrar.py")\n'
        'REGISTRAR_LOG = BASE / "gitlab-relay-registrar.log"\n'
    )
    if anchor not in text:
        raise RuntimeError("postboot constants anchor not found")
    text = text.replace(anchor, insert, 1)

    anchor2 = "def main() -> int:\n"
    helper = '''
def start_relay_registrar() -> int | None:
    if not REGISTRAR.is_file():
        log("relay registrar missing")
        return None
    flags = (
        getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    )
    with REGISTRAR_LOG.open("a", encoding="utf-8") as fh:
        proc = subprocess.Popen(
            [sys.executable, str(REGISTRAR)],
            stdin=subprocess.DEVNULL,
            stdout=fh,
            stderr=fh,
            creationflags=flags,
            close_fds=True,
        )
    log(f"relay registrar launch pid={proc.pid}")
    return int(proc.pid)


'''
    if anchor2 not in text:
        raise RuntimeError("postboot main anchor not found")
    text = text.replace(anchor2, helper + anchor2, 1)

    anchor3 = "        return int(cp.returncode)\n"
    replacement = (
        "        if cp.returncode == 0:\n"
        "            start_relay_registrar()\n"
        "        return int(cp.returncode)\n"
    )
    if anchor3 not in text:
        raise RuntimeError("postboot return anchor not found")
    return text.replace(anchor3, replacement, 1)


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    source = repo_root / "scripts" / "gitlab_relay_registrar.py"
    if not source.is_file() or not POSTBOOT.is_file():
        raise SystemExit("required source or runtime postboot runner missing")

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, TARGET)

    original = POSTBOOT.read_text(encoding="utf-8")
    if not BACKUP.exists():
        BACKUP.write_text(original, encoding="utf-8", newline="\n")
    patched = patch_postboot(original)
    POSTBOOT.write_text(patched, encoding="utf-8", newline="\n")

    py_compile.compile(str(TARGET), doraise=True)
    py_compile.compile(str(POSTBOOT), doraise=True)
    print(json.dumps({
        "result": "installed",
        "registrar": str(TARGET),
        "postboot": str(POSTBOOT),
        "backup": str(BACKUP),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path

RUNTIME_ENV = Path(os.environ.get(
    "PC24X7_ENV_FILE",
    r"C:\Users\Windows\AppData\Local\ReqSys\TodoGlobal24x7\runtime.env",
))
RECIPIENT_CERT = Path(os.environ.get(
    "NOTERI_RECIPIENT_CERT",
    r"C:\Users\Windows\AppData\Local\ReqSys\TodoGlobal24x7\noteri-recipient.crt",
))
OPENSSL = Path(r"C:\Program Files\Git\usr\bin\openssl.exe")
REMOTE_HOST = os.environ.get("NOTERI_DB_REMOTE_HOST", "192.168.1.45")
REMOTE_PORT = os.environ.get("NOTERI_DB_REMOTE_PORT", "15432")


def main() -> int:
    if not RUNTIME_ENV.is_file() or not RECIPIENT_CERT.is_file() or not OPENSSL.is_file():
        raise SystemExit("required local input missing")
    database_url = ""
    for line in RUNTIME_ENV.read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            database_url = line.split("=", 1)[1].strip()
            break
    if not database_url:
        raise SystemExit("DATABASE_URL missing")
    remote_url = database_url.replace("@db:5432/", f"@{REMOTE_HOST}:{REMOTE_PORT}/")
    if remote_url == database_url:
        raise SystemExit("DATABASE_URL host rewrite not applied")
    plaintext = f"DATABASE_URL={remote_url}\n".encode("utf-8")
    result = subprocess.run(
        [str(OPENSSL), "cms", "-encrypt", "-binary", "-aes-256-cbc", "-outform", "DER", str(RECIPIENT_CERT)],
        input=plaintext,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit("openssl cms encryption failed")
    print(base64.b64encode(result.stdout).decode("ascii"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

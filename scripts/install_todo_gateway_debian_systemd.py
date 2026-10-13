#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote


PILOT_PROFILE = "pilot-dev-isolated"
SHA40 = re.compile(r"^[0-9a-f]{40}$")
PILOT_ID = re.compile(r"^[a-z][a-z0-9-]{2,31}$")
FORBIDDEN_PORTS = {8000, 5432}
FORBIDDEN_NAMES = {
    "todo-global", "todo-global-gateway.service",
    "todo_global_bus_dev", "todo_global_bus_dev_user",
}


def load_pilot_manifest(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "schema_version", "profile", "pilot_id", "host", "environment",
        "source_sha", "release_sha", "bind_host", "gateway_port",
        "postgres_port", "service_name", "database_name", "database_role",
        "base_dir", "config_dir", "evidence_dir", "ownership_marker",
        "secrets_transport",
    }
    if set(data) != required:
        raise ValueError("pilot_manifest_fields_invalid")
    if data["schema_version"] != 1 or data["profile"] != PILOT_PROFILE:
        raise ValueError("pilot_manifest_contract_invalid")
    if data["environment"] != "dev" or not PILOT_ID.fullmatch(str(data["pilot_id"])):
        raise ValueError("pilot_identity_invalid")
    if not SHA40.fullmatch(str(data["source_sha"])) or data["release_sha"] != data["source_sha"]:
        raise ValueError("pilot_release_sha_invalid")
    if data["bind_host"] != "127.0.0.1":
        raise ValueError("pilot_bind_must_be_loopback")
    ports = (data["gateway_port"], data["postgres_port"])
    if any(type(port) is not int or not 1024 <= port <= 65535 for port in ports):
        raise ValueError("pilot_port_invalid")
    if len(set(ports)) != 2 or any(port in FORBIDDEN_PORTS for port in ports):
        raise ValueError("pilot_port_collision")
    names = (data["service_name"], data["database_name"], data["database_role"])
    if any(not isinstance(name, str) or not name or name in FORBIDDEN_NAMES for name in names):
        raise ValueError("pilot_resource_name_invalid")
    prefix = data["pilot_id"].replace("-", "_")
    if (
        data["service_name"] != f"todo-pilot-{data['pilot_id']}.service"
        or data["database_name"] != f"todo_pilot_{prefix}_db"
        or data["database_role"] != f"todo_pilot_{prefix}_role"
    ):
        raise ValueError("pilot_resource_not_namespaced")
    roots = {
        "base_dir": f"/opt/todo-pilots/{data['pilot_id']}",
        "config_dir": f"/etc/todo-pilots/{data['pilot_id']}",
        "evidence_dir": f"/var/lib/todo-pilots/{data['pilot_id']}/evidence",
    }
    for key, expected in roots.items():
        if data[key] != expected:
            raise ValueError("pilot_path_not_isolated")
    if data["ownership_marker"] != f"{data['base_dir']}/.pilot-ownership.json":
        raise ValueError("pilot_ownership_marker_invalid")
    if data["secrets_transport"] != "protected_file_no_argv":
        raise ValueError("pilot_secrets_transport_invalid")
    if not isinstance(data["host"], str) or not data["host"].strip():
        raise ValueError("pilot_host_invalid")
    return data


def build_pilot_dry_run(manifest: dict, occupied: set[str] | None = None) -> dict:
    occupied = occupied or set()
    resources = {
        manifest["service_name"], manifest["database_name"], manifest["database_role"],
        manifest["base_dir"], manifest["config_dir"], manifest["evidence_dir"],
        str(manifest["gateway_port"]), str(manifest["postgres_port"]),
    }
    collisions = sorted(resources & occupied)
    return {
        "schema_version": 1,
        "profile": PILOT_PROFILE,
        "status": "blocked" if collisions else "planned",
        "dry_run": True,
        "host": manifest["host"],
        "environment": "dev",
        "source_sha": manifest["source_sha"],
        "manifest_sha256": hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "bind_host": "127.0.0.1",
        "resources": sorted(resources),
        "collisions": collisions,
        "ownership_required_before_restart_or_cleanup": True,
        "secrets_in_argv": False,
        "runtime_mutated": False,
        "ready_for_install": False,
    }

APP_USER = "todo-global"
APP_GROUP = "todo-global"
DB_USER = "todo_global_bus_dev_user"
DB_NAME = "todo_global_bus_dev"
BASE = Path("/opt/todo-global")
CONFIG_DIR = Path("/etc/todo-global")
ENV_FILE = CONFIG_DIR / "runtime.env"
UNIT_FILE = Path("/etc/systemd/system/todo-global-gateway.service")


def run(args: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(f"command failed rc={result.returncode}: {args[0]}")
    return result


def require_root() -> None:
    if os.name != "posix" or os.geteuid() != 0:
        raise RuntimeError("installer must run as root on Debian/Linux")


def install_packages() -> None:
    run(["apt-get", "update"])
    run([
        "apt-get", "install", "-y",
        "python3", "python3-venv", "python3-pip",
        "postgresql", "postgresql-client", "ca-certificates",
    ])


def ensure_service_user() -> None:
    if run(["getent", "group", APP_GROUP], check=False).returncode != 0:
        run(["groupadd", "--system", APP_GROUP])
    if run(["id", "-u", APP_USER], check=False).returncode != 0:
        run([
            "useradd", "--system", "--gid", APP_GROUP,
            "--home-dir", BASE.as_posix(), "--shell", "/usr/sbin/nologin", APP_USER,
        ])


def parse_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_FILE.exists():
        return values
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if raw and not raw.lstrip().startswith("#") and "=" in raw:
            key, value = raw.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def ensure_runtime_env() -> tuple[str, str]:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    current = parse_env()
    token = current.get("TODO_GATEWAY_TOKEN") or secrets.token_urlsafe(40)
    password = current.get("POSTGRES_PASSWORD") or secrets.token_urlsafe(32)
    db_url = f"postgresql://{DB_USER}:{quote(password, safe='')}@127.0.0.1:5432/{DB_NAME}"
    ENV_FILE.write_text(
        "\n".join([
            f"POSTGRES_PASSWORD={password}",
            f"DATABASE_URL={db_url}",
            f"TODO_GATEWAY_TOKEN={token}",
            "PORT=8000",
            "LOG_LEVEL=INFO",
            "PYTHONDONTWRITEBYTECODE=1",
            "",
        ]),
        encoding="utf-8",
    )
    os.chmod(ENV_FILE, 0o640)
    shutil.chown(ENV_FILE, user="root", group=APP_GROUP)
    return password, token


def postgres_scalar(sql: str) -> str:
    result = run(["runuser", "-u", "postgres", "--", "psql", "-Atqc", sql, "postgres"])
    return result.stdout.strip()


def ensure_postgres(password: str) -> None:
    run(["systemctl", "enable", "--now", "postgresql"])
    exists = postgres_scalar(f"SELECT 1 FROM pg_roles WHERE rolname='{DB_USER}'") == "1"
    escaped = password.replace("'", "''")
    if exists:
        postgres_scalar(f"ALTER ROLE {DB_USER} WITH LOGIN PASSWORD '{escaped}'")
    else:
        postgres_scalar(f"CREATE ROLE {DB_USER} WITH LOGIN PASSWORD '{escaped}'")
    db_exists = postgres_scalar(f"SELECT 1 FROM pg_database WHERE datname='{DB_NAME}'") == "1"
    if not db_exists:
        run(["runuser", "-u", "postgres", "--", "createdb", "-O", DB_USER, DB_NAME])


def git_revision(repo_root: Path) -> str:
    result = run(["git", "rev-parse", "HEAD"], cwd=repo_root, check=False)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()[:12]
    return time.strftime("manual-%Y%m%d%H%M%S", time.gmtime())


def install_release(repo_root: Path) -> Path:
    release = BASE / "releases" / git_revision(repo_root)
    if release.exists():
        shutil.rmtree(release)
    release.mkdir(parents=True, exist_ok=True)
    for name in ("services", "scripts", "sql"):
        shutil.copytree(repo_root / name, release / name)
    shutil.copy2(repo_root / "services" / "todo_gateway" / "requirements.txt", release / "requirements.txt")
    run(["python3", "-m", "venv", (release / ".venv").as_posix()])
    run([(release / ".venv/bin/python").as_posix(), "-m", "pip", "install", "--no-cache-dir", "-r", (release / "requirements.txt").as_posix()])
    shutil.chown(release, user=APP_USER, group=APP_GROUP)
    for root, dirs, files in os.walk(release):
        for name in dirs:
            shutil.chown(Path(root) / name, user=APP_USER, group=APP_GROUP)
        for name in files:
            shutil.chown(Path(root) / name, user=APP_USER, group=APP_GROUP)
    current = BASE / "current"
    temp_link = BASE / ".current.new"
    if temp_link.exists() or temp_link.is_symlink():
        temp_link.unlink()
    temp_link.symlink_to(release, target_is_directory=True)
    temp_link.replace(current)
    return release


def install_unit() -> None:
    UNIT_FILE.write_text(
        """[Unit]\nDescription=TODO Global Gateway\nAfter=network-online.target postgresql.service\nWants=network-online.target postgresql.service\n\n[Service]\nType=simple\nUser=todo-global\nGroup=todo-global\nWorkingDirectory=/opt/todo-global/current\nEnvironmentFile=/etc/todo-global/runtime.env\nExecStart=/opt/todo-global/current/.venv/bin/python -m services.todo_gateway.service_main\nRestart=always\nRestartSec=5\nTimeoutStopSec=20\nNoNewPrivileges=true\nPrivateTmp=true\nProtectHome=true\nProtectSystem=strict\n\n[Install]\nWantedBy=multi-user.target\n""",
        encoding="utf-8",
    )
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", "todo-global-gateway.service"])


def smoke() -> bool:
    import urllib.request
    for _ in range(60):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8000/readyz", timeout=3) as response:
                if response.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--skip-packages", action="store_true")
    parser.add_argument("--pilot-manifest", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.pilot_manifest is not None:
        if not args.dry_run:
            raise RuntimeError("pilot_profile_requires_dry_run")
        manifest = load_pilot_manifest(args.pilot_manifest)
        print(json.dumps(build_pilot_dry_run(manifest), sort_keys=True))
        return 0
    if args.dry_run:
        raise RuntimeError("dry_run_requires_pilot_manifest")
    require_root()
    repo_root = Path(args.repo_root).resolve()
    if not args.skip_packages:
        install_packages()
    ensure_service_user()
    password, _ = ensure_runtime_env()
    ensure_postgres(password)
    release = install_release(repo_root)
    install_unit()
    ready = smoke()
    print(f"TODO_GLOBAL_DEBIAN_READY={str(ready).lower()} release={release.name}")
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())

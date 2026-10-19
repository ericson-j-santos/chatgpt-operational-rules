#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import grp
import hashlib
import json
import os
import pwd
import re
import secrets
import signal
import shutil
import stat
import subprocess
import sys
import time
from collections import namedtuple
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator
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
    inventory_verified = occupied is not None
    occupied_resources = set() if occupied is None else occupied
    resources = {
        manifest["service_name"], manifest["database_name"], manifest["database_role"],
        manifest["base_dir"], manifest["config_dir"], manifest["evidence_dir"],
        str(manifest["gateway_port"]), str(manifest["postgres_port"]),
    }
    collisions = sorted(resources & occupied_resources)
    status = "blocked" if collisions else ("planned" if inventory_verified else "static_plan")
    return {
        "schema_version": 1,
        "profile": PILOT_PROFILE,
        "status": status,
        "inventory_verified": inventory_verified,
        "collisions_status": "verified" if inventory_verified else "not_verified",
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
COMMAND_TIMEOUT_SECONDS = 900
PSQL_TIMEOUT_SECONDS = 30
PROCESS_TERMINATE_GRACE_SECONDS = 5
INSTALL_LOCK_TIMEOUT_SECONDS = 30.0
INSTALL_LOCK_POLL_SECONDS = 0.05
RELEASE_MARKER_NAME = ".todo-release.json"
RELEASE_MARKER_OWNER = "todo-global-debian-systemd"
ACTIVATION_JOURNAL_NAME = ".todo-activation.json"
RELEASE_ID = re.compile(r"^(?:[0-9a-f]{12}|manual-[0-9]{14})$")


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    input_text: str | None = None,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    command_name = Path(args[0]).name if args else "unknown"
    try:
        process = subprocess.Popen(
            args,
            cwd=cwd,
            text=True,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=PROCESS_TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
        else:
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                pass
            else:
                os.killpg(process.pid, signal.SIGKILL)
        raise RuntimeError(f"command timed out: {command_name}") from None
    except OSError:
        raise RuntimeError(f"command could not start: {command_name}") from None
    result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    if check and result.returncode != 0:
        raise RuntimeError(f"command failed rc={result.returncode}: {command_name}")
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
    sql_input = sql if sql.endswith("\n") else sql + "\n"
    result = run(
        [
            "runuser", "-u", "postgres", "--", "psql",
            "--no-psqlrc", "--no-password", "--set=ON_ERROR_STOP=1",
            "--tuples-only", "--no-align", "--quiet", "--dbname=postgres",
        ],
        input_text=sql_input,
        timeout=PSQL_TIMEOUT_SECONDS,
    )
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


CurrentSnapshot = namedtuple(
    "CurrentSnapshot",
    ("exists", "raw_target", "normalized_target", "device", "inode"),
)
ReleaseDeployment = namedtuple(
    "ReleaseDeployment",
    ("release", "previous", "activated"),
)


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _absolute_lexical(path: Path) -> Path:
    return Path(os.path.abspath(os.path.normpath(path)))


def _releases_dir() -> Path:
    return BASE / "releases"


def _ensure_real_directory(path: Path) -> None:
    if _lexists(path):
        if path.is_symlink() or not path.is_dir():
            raise RuntimeError("release ownership verification failed")
    else:
        path.mkdir(parents=True, exist_ok=False)
    metadata = path.stat(follow_symlinks=False)
    if (
        metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise RuntimeError("release ownership verification failed")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _current_snapshot() -> CurrentSnapshot:
    current = BASE / "current"
    if not _lexists(current):
        return CurrentSnapshot(False, None, None, None, None)
    if not current.is_symlink():
        raise RuntimeError("current ownership verification failed")
    raw_target = os.readlink(current)
    target = Path(raw_target)
    if not target.is_absolute():
        raise RuntimeError("current ownership verification failed")
    normalized = _absolute_lexical(target)
    releases = _absolute_lexical(_releases_dir())
    if raw_target != normalized.as_posix() or normalized.parent != releases:
        raise RuntimeError("current ownership verification failed")
    metadata = current.lstat()
    if metadata.st_uid != os.geteuid() or metadata.st_gid != os.getegid():
        raise RuntimeError("current ownership verification failed")
    return CurrentSnapshot(
        True,
        raw_target,
        normalized.as_posix(),
        metadata.st_dev,
        metadata.st_ino,
    )


def _assert_current_unchanged(expected: CurrentSnapshot) -> None:
    if _current_snapshot() != expected:
        raise RuntimeError("concurrent current update detected")


def _release_marker_payload(revision: str, state: str, nonce: str) -> dict:
    return {
        "schema_version": 1,
        "owner": RELEASE_MARKER_OWNER,
        "release": revision,
        "state": state,
        "nonce": nonce,
    }


def _write_release_marker(release: Path, revision: str, state: str, nonce: str) -> None:
    marker = release / RELEASE_MARKER_NAME
    temporary = release / f".{RELEASE_MARKER_NAME}.{nonce}.tmp"
    payload = _release_marker_payload(revision, state, nonce)
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        temporary.replace(marker)
        _fsync_directory(release)
    finally:
        if _lexists(temporary):
            temporary.unlink()


def _activation_journal_path() -> Path:
    return BASE / ACTIVATION_JOURNAL_NAME


def _write_activation_journal(
    candidate: Path,
    previous: CurrentSnapshot,
    nonce: str,
) -> None:
    journal = _activation_journal_path()
    temporary = BASE / f".{ACTIVATION_JOURNAL_NAME}.{nonce}.tmp"
    if _lexists(journal):
        raise RuntimeError("pending activation recovery required")
    payload = {
        "schema_version": 1,
        "owner": RELEASE_MARKER_OWNER,
        "candidate": candidate.as_posix(),
        "previous_raw_target": previous.raw_target if previous.exists else None,
        "nonce": nonce,
    }
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        temporary.replace(journal)
        _fsync_directory(BASE)
    finally:
        if _lexists(temporary):
            temporary.unlink()


def _load_activation_journal() -> dict:
    journal = _activation_journal_path()
    try:
        metadata = journal.lstat()
    except OSError as exc:
        raise RuntimeError("activation journal ownership verification failed") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise RuntimeError("activation journal ownership verification failed")
    try:
        data = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("activation journal verification failed") from exc
    if not isinstance(data, dict):
        raise RuntimeError("activation journal verification failed")
    return data


def _clear_activation_journal(nonce: str) -> None:
    journal = _activation_journal_path()
    if not _lexists(journal):
        return
    data = _load_activation_journal()
    if data.get("nonce") != nonce:
        raise RuntimeError("activation journal verification failed")
    journal.unlink()
    _fsync_directory(BASE)


def _load_release_marker(release: Path) -> dict:
    marker = release / RELEASE_MARKER_NAME
    try:
        metadata = marker.lstat()
    except OSError as exc:
        raise RuntimeError("release ownership verification failed") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise RuntimeError("release ownership verification failed")
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("release ownership verification failed") from exc
    if not isinstance(data, dict):
        raise RuntimeError("release ownership verification failed")
    return data


def _validate_release_layout(release: Path) -> None:
    for relative in ("services", "scripts", "sql", ".venv", ".venv/bin"):
        candidate = release / relative
        if candidate.is_symlink() or not candidate.is_dir():
            raise RuntimeError("release completeness verification failed")
    requirements = release / "requirements.txt"
    python = release / ".venv/bin/python"
    if requirements.is_symlink() or not requirements.is_file():
        raise RuntimeError("release completeness verification failed")
    if not python.is_file() or not os.access(python, os.X_OK):
        raise RuntimeError("release completeness verification failed")


def _validate_release_root_owner(release: Path, uid: int, gid: int) -> None:
    try:
        metadata = release.stat(follow_symlinks=False)
    except OSError as exc:
        raise RuntimeError("release ownership verification failed") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != uid
        or metadata.st_gid != gid
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise RuntimeError("release ownership verification failed")


def _app_identity() -> tuple[int, int]:
    try:
        return pwd.getpwnam(APP_USER).pw_uid, grp.getgrnam(APP_GROUP).gr_gid
    except KeyError as exc:
        raise RuntimeError("service account ownership unavailable") from exc


def _validate_ready_release(release: Path, revision: str) -> None:
    if (
        release.parent != _releases_dir()
        or release.name != revision
        or release.is_symlink()
        or not release.is_dir()
    ):
        raise RuntimeError("release ownership verification failed")
    _validate_release_root_owner(release, os.geteuid(), os.getegid())
    _validate_release_layout(release)
    data = _load_release_marker(release)
    if (
        set(data) != {"schema_version", "owner", "release", "state", "nonce"}
        or data.get("schema_version") != 1
        or data.get("owner") != RELEASE_MARKER_OWNER
        or data.get("release") != revision
        or data.get("state") != "ready"
        or not re.fullmatch(r"[0-9a-f]{32}", str(data.get("nonce", "")))
    ):
        raise RuntimeError("release ownership verification failed")


def _validate_active_release(snapshot: CurrentSnapshot) -> None:
    if not snapshot.exists:
        return
    assert snapshot.normalized_target is not None
    active = Path(snapshot.normalized_target)
    if active.is_symlink() or not active.is_dir() or not RELEASE_ID.fullmatch(active.name):
        raise RuntimeError("current ownership verification failed")
    marker = active / RELEASE_MARKER_NAME
    if _lexists(marker):
        _validate_ready_release(active, active.name)
    else:
        uid, gid = _app_identity()
        _validate_release_root_owner(active, uid, gid)
        _validate_release_layout(active)


def _validate_existing_release(
    release: Path,
    revision: str,
    current: CurrentSnapshot,
) -> None:
    release_path = _absolute_lexical(release).as_posix()
    if current.normalized_target == release_path and not _lexists(release / RELEASE_MARKER_NAME):
        uid, gid = _app_identity()
        _validate_release_root_owner(release, uid, gid)
        _validate_release_layout(release)
        return
    _validate_ready_release(release, revision)


@contextmanager
def _installation_lock() -> Iterator[None]:
    _ensure_real_directory(BASE)
    _ensure_real_directory(_releases_dir())
    lock_path = BASE / ".install.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise RuntimeError("release installation lock unavailable") from exc
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        os.close(descriptor)
        raise RuntimeError("release installation lock ownership invalid")
    deadline = time.monotonic() + INSTALL_LOCK_TIMEOUT_SECONDS
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("concurrent release installation detected") from None
                time.sleep(INSTALL_LOCK_POLL_SECONDS)
        yield
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _chown_release_tree(release: Path) -> None:
    uid, gid = _app_identity()
    for root, dirs, files in os.walk(release):
        for name in [*dirs, *files]:
            if name == RELEASE_MARKER_NAME:
                continue
            os.chown(Path(root) / name, uid, gid, follow_symlinks=False)


def _rename_no_replace(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise RuntimeError("atomic no-clobber release publication unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(destination),
        1,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in {errno.EEXIST, errno.ENOTEMPTY}:
        raise RuntimeError("concurrent release publication detected") from None
    raise RuntimeError("atomic release publication failed") from None


def _cleanup_owned_staging(staging: Path, revision: str, nonce: str) -> None:
    expected_parent = _releases_dir() / ".staging"
    if staging.parent != expected_parent or staging.is_symlink() or not staging.is_dir():
        raise RuntimeError("staging ownership verification failed")
    _validate_release_root_owner(staging, os.geteuid(), os.getegid())
    data = _load_release_marker(staging)
    if (
        set(data) != {"schema_version", "owner", "release", "state", "nonce"}
        or data.get("schema_version") != 1
        or data.get("owner") != RELEASE_MARKER_OWNER
        or data.get("release") != revision
        or data.get("state") not in {"staging", "ready"}
        or data.get("nonce") != nonce
    ):
        raise RuntimeError("staging ownership verification failed")
    current = _current_snapshot()
    if current.normalized_target == _absolute_lexical(staging).as_posix():
        raise RuntimeError("staging is active and cannot be removed")
    shutil.rmtree(staging)


def _recover_owned_staging() -> None:
    staging_root = _releases_dir() / ".staging"
    _ensure_real_directory(staging_root)
    removed = False
    for staging in sorted(staging_root.iterdir()):
        try:
            revision, nonce = staging.name.rsplit("-", 1)
        except ValueError:
            raise RuntimeError("staging ownership verification failed") from None
        if not RELEASE_ID.fullmatch(revision) or not re.fullmatch(r"[0-9a-f]{32}", nonce):
            raise RuntimeError("staging ownership verification failed")
        _cleanup_owned_staging(staging, revision, nonce)
        removed = True
    if removed:
        _fsync_directory(staging_root)


def _prepare_release(
    repo_root: Path,
    revision: str,
    initial_current: CurrentSnapshot,
) -> Path:
    release = _releases_dir() / revision
    if _lexists(release):
        _validate_existing_release(release, revision, initial_current)
        return release

    staging_root = _releases_dir() / ".staging"
    _ensure_real_directory(staging_root)
    nonce = secrets.token_hex(16)
    staging = staging_root / f"{revision}-{nonce}"
    staging.mkdir(mode=0o700, exist_ok=False)
    try:
        _write_release_marker(staging, revision, "staging", nonce)
    except BaseException:
        staging.rmdir()
        raise

    try:
        for name in ("services", "scripts", "sql"):
            shutil.copytree(repo_root / name, staging / name)
        shutil.copy2(
            repo_root / "services" / "todo_gateway" / "requirements.txt",
            staging / "requirements.txt",
        )
        run(["python3", "-m", "venv", (staging / ".venv").as_posix()])
        run([
            (staging / ".venv/bin/python").as_posix(),
            "-m", "pip", "install", "--no-cache-dir", "-r",
            (staging / "requirements.txt").as_posix(),
        ])
        _chown_release_tree(staging)
        _write_release_marker(staging, revision, "ready", nonce)
        os.chmod(staging, 0o755)
        _assert_current_unchanged(initial_current)
        _rename_no_replace(staging, release)
        _fsync_directory(_releases_dir())
        return release
    except BaseException:
        if _lexists(staging):
            _cleanup_owned_staging(staging, revision, nonce)
        raise


def _replace_current(raw_target: str | None, expected: CurrentSnapshot) -> CurrentSnapshot:
    _assert_current_unchanged(expected)
    current = BASE / "current"
    if raw_target is None:
        if expected.exists:
            current.unlink()
            _fsync_directory(BASE)
        return _current_snapshot()

    temporary = BASE / f".current-{os.getpid()}-{secrets.token_hex(8)}.tmp"
    temporary.symlink_to(raw_target, target_is_directory=True)
    try:
        _assert_current_unchanged(expected)
        os.replace(temporary, current)
        _fsync_directory(BASE)
    finally:
        if _lexists(temporary):
            temporary.unlink()
    updated = _current_snapshot()
    target = Path(raw_target)
    if not target.is_absolute():
        target = current.parent / target
    if updated.normalized_target != _absolute_lexical(target).as_posix():
        raise RuntimeError("current activation verification failed")
    return updated


def _validated_journal_target(raw_target: str, *, candidate: bool) -> Path:
    target = Path(raw_target)
    normalized = _absolute_lexical(target)
    if (
        not target.is_absolute()
        or raw_target != normalized.as_posix()
        or normalized.parent != _absolute_lexical(_releases_dir())
        or not RELEASE_ID.fullmatch(normalized.name)
    ):
        raise RuntimeError("activation journal target verification failed")
    if not candidate:
        marker = normalized / RELEASE_MARKER_NAME
        if _lexists(marker):
            _validate_ready_release(normalized, normalized.name)
        else:
            uid, gid = _app_identity()
            _validate_release_root_owner(normalized, uid, gid)
            _validate_release_layout(normalized)
    return normalized


def _recover_pending_activation(
    restore_hook: Callable[[bool], None] | None,
) -> None:
    if not _lexists(_activation_journal_path()):
        return
    data = _load_activation_journal()
    if (
        set(data)
        != {"schema_version", "owner", "candidate", "previous_raw_target", "nonce"}
        or data.get("schema_version") != 1
        or data.get("owner") != RELEASE_MARKER_OWNER
        or not isinstance(data.get("candidate"), str)
        or data.get("previous_raw_target") is not None
        and not isinstance(data.get("previous_raw_target"), str)
        or not re.fullmatch(r"[0-9a-f]{32}", str(data.get("nonce", "")))
    ):
        raise RuntimeError("activation journal verification failed")
    candidate = _validated_journal_target(data["candidate"], candidate=True)
    previous_raw = data["previous_raw_target"]
    previous = (
        _validated_journal_target(previous_raw, candidate=False)
        if previous_raw is not None
        else None
    )
    current = _current_snapshot()
    previous_path = previous.as_posix() if previous is not None else None
    if current.normalized_target == candidate.as_posix():
        current = _replace_current(previous_raw, current)
    if current.normalized_target != previous_path:
        raise RuntimeError("activation recovery current mismatch")
    if restore_hook is not None:
        restore_hook(previous is not None)
    _clear_activation_journal(data["nonce"])


def recover_pending_activation(
    restore_hook: Callable[[bool], None] | None = None,
) -> None:
    with _installation_lock():
        _recover_pending_activation(restore_hook)


@contextmanager
def _release_transaction_under_install_lock(
    repo_root: Path,
    *,
    restore_hook: Callable[[bool], None] | None = None,
) -> Iterator[ReleaseDeployment]:
    revision = git_revision(repo_root)
    if not RELEASE_ID.fullmatch(revision):
        raise RuntimeError("release revision invalid")
    initial_current = _current_snapshot()
    _validate_active_release(initial_current)
    _recover_owned_staging()
    release = _prepare_release(repo_root, revision, initial_current)
    _assert_current_unchanged(initial_current)
    release_path = _absolute_lexical(release).as_posix()
    should_activate = initial_current.normalized_target != release_path
    journal_nonce: str | None = None
    entered = False
    try:
        if should_activate:
            _validate_ready_release(release, revision)
            journal_nonce = secrets.token_hex(16)
            _write_activation_journal(release, initial_current, journal_nonce)
            _replace_current(release.as_posix(), initial_current)
        entered = True
        yield ReleaseDeployment(release, initial_current, should_activate)
        if journal_nonce is not None:
            _clear_activation_journal(journal_nonce)
    except BaseException:
        if should_activate:
            try:
                observed = _current_snapshot()
                if observed != initial_current:
                    if observed.normalized_target != release_path:
                        raise RuntimeError("concurrent current update detected")
                    _replace_current(
                        initial_current.raw_target if initial_current.exists else None,
                        observed,
                    )
                if entered and restore_hook is not None:
                    restore_hook(initial_current.exists)
                if journal_nonce is not None:
                    _clear_activation_journal(journal_nonce)
            except Exception as exc:
                raise RuntimeError("release rollback failed") from exc
        raise


@contextmanager
def release_transaction(
    repo_root: Path,
    *,
    restore_hook: Callable[[bool], None] | None = None,
) -> Iterator[ReleaseDeployment]:
    with _installation_lock():
        _recover_pending_activation(restore_hook)
        with _release_transaction_under_install_lock(
            repo_root,
            restore_hook=restore_hook,
        ) as deployment:
            yield deployment


def install_release(repo_root: Path) -> Path:
    with release_transaction(repo_root) as deployment:
        return deployment.release


def install_unit() -> None:
    UNIT_FILE.write_text(
        """[Unit]\nDescription=TODO Global Gateway\nAfter=network-online.target postgresql.service\nWants=network-online.target postgresql.service\n\n[Service]\nType=simple\nUser=todo-global\nGroup=todo-global\nWorkingDirectory=/opt/todo-global/current\nEnvironmentFile=/etc/todo-global/runtime.env\nExecStart=/opt/todo-global/current/.venv/bin/python -m services.todo_gateway.service_main\nRestart=always\nRestartSec=5\nTimeoutStopSec=20\nNoNewPrivileges=true\nPrivateTmp=true\nProtectHome=true\nProtectSystem=strict\n\n[Install]\nWantedBy=multi-user.target\n""",
        encoding="utf-8",
    )
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "todo-global-gateway.service"])


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


class _ReadinessFailed(RuntimeError):
    pass


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
    repo_root = Path(args.repo_root).resolve()

    def restore_previous_service(previous_exists: bool) -> None:
        command = "restart" if previous_exists else "stop"
        run(["systemctl", command, "todo-global-gateway.service"])

    require_root()
    deployment: ReleaseDeployment | None = None

    try:
        with _installation_lock():
            _recover_pending_activation(restore_previous_service)
            if not args.skip_packages:
                install_packages()
            ensure_service_user()
            password, _ = ensure_runtime_env()
            ensure_postgres(password)
            with _release_transaction_under_install_lock(
                repo_root,
                restore_hook=restore_previous_service,
            ) as deployment:
                install_unit()
                run(["systemctl", "restart", "todo-global-gateway.service"])
                if not smoke():
                    raise _ReadinessFailed("gateway readiness check failed")
    except BaseException as failure:
        if isinstance(failure, _ReadinessFailed):
            assert deployment is not None
            print(f"TODO_GLOBAL_DEBIAN_READY=false release={deployment.release.name}")
            return 1
        raise
    assert deployment is not None
    print(f"TODO_GLOBAL_DEBIAN_READY=true release={deployment.release.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

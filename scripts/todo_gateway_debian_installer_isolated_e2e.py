#!/usr/bin/env python3
"""Homologate the canonical Debian installer on a disposable GitHub-hosted VM."""
from __future__ import annotations

import argparse
import ast
import grp
import hashlib
import json
import os
import pwd
import re
import signal
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import urlopen

REPOSITORY = "ericson-j-santos/chatgpt-operational-rules"
APP_USER = "todo-global"
OWNER = "ericson-j-santos"
REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
DB_USER = "todo_global_bus_dev_user"
DB_NAME = "todo_global_bus_dev"
BASE = Path("/opt/todo-global")
CONFIG_DIR = Path("/etc/todo-global")
STATE_DIR = Path("/var/lib/todo-global")
UNIT_NAME = "todo-global-gateway.service"
UNIT_PATHS = (
    Path("/etc/systemd/system") / UNIT_NAME,
    Path("/lib/systemd/system") / UNIT_NAME,
    Path("/usr/lib/systemd/system") / UNIT_NAME,
)
ENV_FILE = CONFIG_DIR / "runtime.env"
ACTIVATION_JOURNAL = BASE / ".todo-activation.json"
INSTALLER = Path("scripts/install_todo_gateway_debian_systemd.py")
PSQL_ARGS = (
    "--no-psqlrc",
    "--no-password",
    "--set=ON_ERROR_STOP=1",
    "--tuples-only",
    "--no-align",
    "--quiet",
    "--dbname=postgres",
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
CORRELATION_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
SQL_ARG_RE = re.compile(r"\b(?:SELECT|ALTER|CREATE|DROP|GRANT|REVOKE)\b", re.IGNORECASE)
EXECVE_RE = re.compile(
    r'execve\("(?P<path>(?:\\.|[^"\\])*)", '
    r'(?P<argv>\[(?:[^\]]|\\.)*\]), '
)
MAX_TRACE_BYTES = 8 * 1024 * 1024
DIAGNOSTIC_EXECUTABLES = frozenset(
    {
        "apt-get",
        "createdb",
        "git",
        "getent",
        "groupadd",
        "id",
        "psql",
        "python",
        "python3",
        "runuser",
        "strace",
        "systemctl",
        "useradd",
    }
)


class HomologationError(RuntimeError):
    """Expected fail-closed condition with a non-sensitive code."""


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def assert_activation_journal_absent(stage: str) -> None:
    errors = {
        "initial": "activation_journal_after_initial_install",
        "replay": "activation_journal_after_replay",
        "rollback": "activation_journal_not_cleared",
    }
    error = errors.get(stage)
    if error is None:
        raise HomologationError("activation_journal_stage_invalid")
    if _lexists(ACTIVATION_JOURNAL):
        raise HomologationError(error)


def validate_execution_context(
    env: dict[str, str],
    *,
    euid: int,
    pid1_name: str,
    in_container: bool,
    collisions: list[str],
) -> None:
    if env.get("GITHUB_ACTIONS") != "true":
        raise HomologationError("github_actions_required")
    if env.get("TODO22_RUNNER_ENVIRONMENT") != "github-hosted":
        raise HomologationError("github_hosted_runner_required")
    if env.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise HomologationError("repository_mismatch")
    if env.get("TODO22_ACTOR") != OWNER:
        raise HomologationError("github_actor_mismatch")
    if env.get("TODO22_TRIGGERING_ACTOR") != OWNER:
        raise HomologationError("github_triggering_actor_mismatch")
    event_name = env.get("GITHUB_EVENT_NAME")
    source_ref = env.get("TODO22_SOURCE_REF", "")
    full_ref = env.get("TODO22_FULL_REF", "")
    ref_type = env.get("TODO22_REF_TYPE", "")
    if not REF_RE.fullmatch(source_ref):
        raise HomologationError("ref_mismatch")
    if event_name == "pull_request":
        if env.get("TODO22_PR_HEAD_REPOSITORY") != REPOSITORY:
            raise HomologationError("pull_request_repository_mismatch")
        if env.get("TODO22_PR_AUTHOR") != OWNER:
            raise HomologationError("pull_request_owner_mismatch")
        if env.get("TODO22_PR_BASE_REF") != "main":
            raise HomologationError("pull_request_base_mismatch")
        if env.get("TODO22_PR_DRAFT") != "true":
            raise HomologationError("pull_request_draft_required")
    elif event_name == "push":
        if (
            source_ref != "main"
            or full_ref != "refs/heads/main"
            or ref_type != "branch"
        ):
            raise HomologationError("ref_mismatch")
    elif event_name == "workflow_dispatch":
        if ref_type != "branch" or full_ref != f"refs/heads/{source_ref}":
            raise HomologationError("ref_mismatch")
        if source_ref != "main" and env.get("TODO22_MANUAL_PR_VERIFIED") != "true":
            raise HomologationError("ref_mismatch")
    else:
        raise HomologationError("github_event_not_allowed")
    if euid != 0:
        raise HomologationError("root_required")
    if pid1_name != "systemd" or in_container:
        raise HomologationError("isolated_systemd_vm_required")
    if collisions:
        raise HomologationError("target_collision")


def detect_collisions() -> list[str]:
    collisions = [
        path.as_posix()
        for path in (BASE, CONFIG_DIR, STATE_DIR, *UNIT_PATHS)
        if _lexists(path)
    ]
    try:
        pwd.getpwnam(APP_USER)
    except KeyError:
        pass
    else:
        collisions.append(f"user:{APP_USER}")

    unit_query = subprocess.run(
        ["systemctl", "list-unit-files", "--no-legend", UNIT_NAME],
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    if unit_query.returncode == 0 and unit_query.stdout.strip():
        collisions.append(f"unit:{UNIT_NAME}")

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 8000))
    except OSError:
        collisions.append("port:127.0.0.1:8000")
    finally:
        probe.close()
    return sorted(set(collisions))


def preflight(repo_root: Path, expected_sha: str, env: dict[str, str]) -> None:
    if not SHA_RE.fullmatch(expected_sha):
        raise HomologationError("expected_sha_invalid")
    pid1_name = Path("/proc/1/comm").read_text(encoding="utf-8").strip()
    cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="replace")
    in_container = Path("/.dockerenv").exists() or bool(
        re.search(r"(?:docker|containerd|kubepods|lxc)", cgroup, re.IGNORECASE)
    )
    validate_execution_context(
        env,
        euid=os.geteuid(),
        pid1_name=pid1_name,
        in_container=in_container,
        collisions=detect_collisions(),
    )
    if not repo_root.is_dir() or not (repo_root / INSTALLER).is_file():
        raise HomologationError("repository_checkout_missing")
    head = _git(repo_root, "rev-parse", "HEAD").stdout.strip()
    if head != expected_sha:
        raise HomologationError("checkout_head_mismatch")
    if _git(repo_root, "status", "--porcelain").stdout.strip():
        raise HomologationError("checkout_not_clean")
    if shutil.which("sudo") is None:
        raise HomologationError("sudo_missing")


def _run(
    args: list[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    try:
        process = subprocess.Popen(
            args,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError:
        raise HomologationError("command_start_failed") from None
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
        raise HomologationError("command_timeout") from None
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def _checked(
    args: list[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    result = _run(args, cwd=cwd, input_text=input_text, timeout=timeout)
    if result.returncode != 0:
        raise HomologationError("command_failed")
    return result


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _checked(
        ["git", "-c", f"safe.directory={repo_root}", *args],
        cwd=repo_root,
        timeout=60,
    )


def _open_validated_trace(path: Path, flags: int) -> int:
    try:
        descriptor = os.open(
            path,
            flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError:
        raise HomologationError("trace_path_invalid") from None
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        os.close(descriptor)
        raise HomologationError("trace_path_invalid")
    return descriptor


def _prepare_trace_path(path: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(
            path,
            flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError:
        raise HomologationError("trace_path_invalid") from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
            or metadata.st_nlink != 1
        ):
            raise HomologationError("trace_path_invalid")
        os.fchmod(descriptor, 0o600)
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
            raise HomologationError("trace_path_invalid")
    except OSError:
        raise HomologationError("trace_path_invalid") from None
    finally:
        os.close(descriptor)


def _read_trace(path: Path) -> str:
    descriptor = _open_validated_trace(path, os.O_RDONLY)
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8", errors="replace") as trace:
            value = trace.read(MAX_TRACE_BYTES + 1)
    except OSError:
        raise HomologationError("trace_read_failed") from None
    if len(value.encode("utf-8")) > MAX_TRACE_BYTES:
        raise HomologationError("trace_too_large")
    return value


def _run_installer(
    repo_root: Path,
    trace_path: Path,
    *,
    skip_packages: bool,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    if shutil.which("strace") is None:
        raise HomologationError("strace_missing")
    _prepare_trace_path(trace_path)
    args = [
        "strace",
        "-f",
        "--kill-on-exit",
        "-qq",
        "-s",
        "4096",
        "-e",
        "trace=execve",
        "-o",
        trace_path.as_posix(),
        sys.executable,
        (repo_root / INSTALLER).as_posix(),
        "--repo-root",
        repo_root.as_posix(),
    ]
    if skip_packages:
        args.append("--skip-packages")
    return _run(args, timeout=timeout)


def _diagnostic_executable(value: str) -> str:
    name = Path(value).name
    return name if name in DIAGNOSTIC_EXECUTABLES else "other"


def _last_exec_diagnostic(trace_path: Path) -> str:
    try:
        last = "none"
        for line in _read_trace(trace_path).splitlines():
            match = EXECVE_RE.search(line)
            if match:
                decoded = ast.literal_eval(f'"{match.group("path")}"')
                last = _diagnostic_executable(decoded)
        return last
    except (HomologationError, SyntaxError, ValueError):
        return "unavailable"


def installer_failure_diagnostic(
    result: subprocess.CompletedProcess[str],
    trace_path: Path,
    expected_stdout: str,
) -> dict[str, object]:
    stderr = result.stderr or ""
    failure_kind = "unexpected_process_contract"
    failed_command = "none"
    command_returncode: int | None = None
    command_failure = re.search(
        r"RuntimeError: command failed rc=(\d+): ([A-Za-z0-9._+-]+)",
        stderr,
    )
    if command_failure:
        failure_kind = "installer_command_failed"
        command_returncode = int(command_failure.group(1))
        failed_command = _diagnostic_executable(command_failure.group(2))
    elif "RuntimeError: command timed out:" in stderr:
        failure_kind = "installer_command_timeout"
    elif "RuntimeError: command could not start:" in stderr:
        failure_kind = "installer_command_start_failed"
    elif "release ownership verification failed" in stderr:
        failure_kind = "release_directory_ownership_rejected"
    elif "PTRACE_O_EXITKILL is not supported" in stderr:
        failure_kind = "tracer_exitkill_unsupported"
    elif "strace:" in stderr:
        failure_kind = "tracer_failed"
    elif result.returncode == 0 and (result.stdout or "").strip() != expected_stdout:
        failure_kind = "stdout_contract_mismatch"
    elif result.returncode == 0 and stderr.strip():
        failure_kind = "unexpected_stderr"

    return {
        "returncode": int(result.returncode),
        "stdout_contract_match": (result.stdout or "").strip() == expected_stdout,
        "stderr_empty": not stderr.strip(),
        "failure_kind": failure_kind,
        "failed_command": failed_command,
        "command_returncode": command_returncode,
        "last_exec": _last_exec_diagnostic(trace_path),
    }


def parse_psql_exec_argv(trace_text: str) -> list[list[str]]:
    commands: list[list[str]] = []
    for line in trace_text.splitlines():
        match = EXECVE_RE.search(line)
        if not match or Path(ast.literal_eval(f'"{match.group("path")}"')).name != "psql":
            continue
        try:
            argv = ast.literal_eval(match.group("argv"))
        except (SyntaxError, ValueError):
            raise HomologationError("trace_parse_failed") from None
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            raise HomologationError("trace_parse_failed")
        commands.append(argv)
    return commands


def audit_psql_execs(trace_texts: list[str], secrets: list[str]) -> int:
    commands = [
        command
        for trace_text in trace_texts
        for command in parse_psql_exec_argv(trace_text)
    ]
    if not commands:
        raise HomologationError("psql_exec_missing")
    for command in commands:
        if any(SQL_ARG_RE.search(argument) for argument in command):
            raise HomologationError("sql_present_in_argv")
        if any(secret and secret in argument for secret in secrets for argument in command):
            raise HomologationError("secret_present_in_argv")
        if tuple(command[1:]) != PSQL_ARGS:
            raise HomologationError("psql_argv_contract_failed")
    return len(commands)


def assert_sensitive_values_absent(texts: list[str], secrets: list[str]) -> None:
    if any(secret and secret in text for secret in secrets for text in texts):
        raise HomologationError("secret_present_in_output")


def assert_local_database_url(values: dict[str, str]) -> None:
    try:
        parsed = urlsplit(values["DATABASE_URL"])
        port = parsed.port
    except (KeyError, ValueError):
        raise HomologationError("runtime_database_url_not_local") from None
    if (
        parsed.scheme != "postgresql"
        or parsed.hostname != "127.0.0.1"
        or port != 5432
        or unquote(parsed.username or "") != DB_USER
        or unquote(parsed.password or "") != values["POSTGRES_PASSWORD"]
        or parsed.path != f"/{DB_NAME}"
        or parsed.query
        or parsed.fragment
    ):
        raise HomologationError("runtime_database_url_not_local")


def read_runtime_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if raw and not raw.lstrip().startswith("#") and "=" in raw:
            key, value = raw.split("=", 1)
            values[key.strip()] = value.strip()
    required = ("POSTGRES_PASSWORD", "DATABASE_URL", "TODO_GATEWAY_TOKEN")
    if any(not values.get(key) for key in required):
        raise HomologationError("runtime_env_incomplete")
    metadata = ENV_FILE.stat()
    expected_gid = grp.getgrnam(APP_USER).gr_gid
    if (
        stat.S_IMODE(metadata.st_mode) != 0o640
        or metadata.st_uid != 0
        or metadata.st_gid != expected_gid
    ):
        raise HomologationError("runtime_env_permissions_invalid")
    assert_local_database_url(values)
    return values


def tree_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    paths = [root, *sorted(root.rglob("*"))]
    for path in paths:
        metadata = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        digest.update(
            json.dumps(
                {
                    "path": relative,
                    "mode": stat.S_IFMT(metadata.st_mode) | stat.S_IMODE(metadata.st_mode),
                    "uid": metadata.st_uid,
                    "gid": metadata.st_gid,
                    "device": metadata.st_dev,
                    "inode": metadata.st_ino,
                    "size": metadata.st_size,
                    "target": os.readlink(path) if path.is_symlink() else None,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if path.is_file() and not path.is_symlink():
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def assert_replay_unchanged(before: dict[str, object], after: dict[str, object]) -> None:
    if before != after:
        raise HomologationError("replay_mutated_release_or_current")


def release_state(release: Path) -> dict[str, object]:
    release_stat = release.stat()
    current = BASE / "current"
    current_stat = current.lstat()
    return {
        "release_device": release_stat.st_dev,
        "release_inode": release_stat.st_ino,
        "release_fingerprint": tree_fingerprint(release),
        "current_device": current_stat.st_dev,
        "current_inode": current_stat.st_ino,
        "current_target": os.readlink(current),
    }


def service_ready(wait_seconds: int = 45) -> bool:
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        active = _run(["systemctl", "is-active", "--quiet", UNIT_NAME], timeout=10)
        if active.returncode == 0:
            try:
                with urlopen("http://127.0.0.1:8000/readyz", timeout=3) as response:
                    if response.status == 200:
                        return True
            except Exception:
                pass
        time.sleep(1)
    return False


def independent_database_readback() -> bool:
    sql = (
        "SELECT current_database() || '|' || "
        "COALESCE(to_regclass('todo_bus.queue_events')::text, '') || '|' || "
        f"COALESCE((SELECT rolname FROM pg_roles WHERE rolname='{DB_USER}'), '');\n"
    )
    result = _run(
        [
            "runuser",
            "-u",
            "postgres",
            "--",
            "psql",
            "--no-psqlrc",
            "--no-password",
            "--set=ON_ERROR_STOP=1",
            "--tuples-only",
            "--no-align",
            "--quiet",
            f"--dbname={DB_NAME}",
        ],
        input_text=sql,
        timeout=30,
    )
    return result.returncode == 0 and result.stdout.strip() == (
        f"{DB_NAME}|todo_bus.queue_events|{DB_USER}"
    )


def make_failing_candidate(repo_root: Path, temp_root: Path) -> tuple[Path, str]:
    candidate = Path(tempfile.mkdtemp(prefix="candidate-", dir=temp_root))
    candidate.rmdir()
    _git(repo_root, "worktree", "add", "--detach", candidate.as_posix(), "HEAD")
    service_main = candidate / "services" / "todo_gateway" / "service_main.py"
    service_main.write_text(
        'raise RuntimeError("todo22 isolated readiness control")\n',
        encoding="utf-8",
    )
    _git(candidate, "add", service_main.relative_to(candidate).as_posix())
    _checked(
        [
            "git",
            "-c",
            f"safe.directory={candidate}",
            "-c",
            "user.name=TODO-22 isolated CI",
            "-c",
            "user.email=todo22-ci.invalid",
            "commit",
            "-m",
            "test: inject isolated readiness failure",
        ],
        cwd=candidate,
        timeout=60,
    )
    revision = _git(candidate, "rev-parse", "HEAD").stdout.strip()
    if not SHA_RE.fullmatch(revision):
        raise HomologationError("candidate_revision_invalid")
    return candidate, revision


def remove_candidate(repo_root: Path, candidate: Path | None, temp_root: Path) -> None:
    if candidate is None:
        return
    try:
        candidate.relative_to(temp_root)
    except ValueError:
        return
    _run(
        [
            "git",
            "-c",
            f"safe.directory={repo_root}",
            "worktree",
            "remove",
            "--force",
            candidate.as_posix(),
        ],
        cwd=repo_root,
        timeout=60,
    )


def write_evidence(path: Path, evidence: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def homologate(
    repo_root: Path,
    expected_sha: str,
    correlation_id: str,
    evidence_path: Path,
    env: dict[str, str],
) -> int:
    evidence: dict[str, object] = {
        "contract": "todo-global-debian-installer-isolated-e2e",
        "generated_at": datetime.now(UTC).isoformat(),
        "repository": REPOSITORY,
        "head_sha": expected_sha,
        "ref": env.get("TODO22_SOURCE_REF", ""),
        "correlation_id": correlation_id,
        "environment": "github-hosted-ubuntu-24.04-disposable",
        "pilot_runtime_mutated": False,
        "ready": False,
    }
    temp_root = Path(env.get("RUNNER_TEMP", tempfile.gettempdir())).resolve()
    trace_root: Path | None = None
    traces: list[Path] = []
    candidate: Path | None = None
    logs: list[str] = []
    try:
        preflight(repo_root, expected_sha, env)
        trace_root = Path(tempfile.mkdtemp(prefix="todo22-installer-audit-", dir=temp_root))
        os.chmod(trace_root, 0o700)
        trace_metadata = trace_root.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(trace_metadata.st_mode)
            or trace_metadata.st_uid != os.geteuid()
            or trace_metadata.st_gid != os.getegid()
            or stat.S_IMODE(trace_metadata.st_mode) != 0o700
        ):
            raise HomologationError("trace_directory_invalid")
        traces = [
            trace_root / f"todo22-installer-{name}.strace"
            for name in ("initial", "replay", "failure")
        ]
        initial = _run_installer(repo_root, traces[0], skip_packages=False, timeout=900)
        logs.extend([initial.stdout, initial.stderr])
        expected_ready = f"TODO_GLOBAL_DEBIAN_READY=true release={expected_sha[:12]}"
        if (
            initial.returncode != 0
            or initial.stdout.strip() != expected_ready
            or initial.stderr.strip()
        ):
            evidence["initial_process_diagnostic"] = installer_failure_diagnostic(
                initial,
                traces[0],
                expected_ready,
            )
            raise HomologationError("initial_install_failed")
        release = BASE / "releases" / expected_sha[:12]
        if not release.is_dir() or not service_ready():
            raise HomologationError("initial_install_not_ready")
        assert_activation_journal_absent("initial")
        runtime = read_runtime_env()
        secrets = [
            runtime["POSTGRES_PASSWORD"],
            runtime["TODO_GATEWAY_TOKEN"],
            runtime["DATABASE_URL"],
        ]
        if not independent_database_readback():
            raise HomologationError("database_readback_failed")

        sentinel = release / "operator-sentinel.txt"
        sentinel.write_text("todo22-preserve-me\n", encoding="utf-8")
        account = pwd.getpwnam(APP_USER)
        os.chown(sentinel, account.pw_uid, account.pw_gid)
        os.chmod(sentinel, 0o600)
        before_replay = release_state(release)

        replay = _run_installer(repo_root, traces[1], skip_packages=True, timeout=300)
        logs.extend([replay.stdout, replay.stderr])
        if (
            replay.returncode != 0
            or replay.stdout.strip() != expected_ready
            or replay.stderr.strip()
        ):
            evidence["replay_process_diagnostic"] = installer_failure_diagnostic(
                replay,
                traces[1],
                expected_ready,
            )
            raise HomologationError("replay_failed")
        after_replay = release_state(release)
        assert_replay_unchanged(before_replay, after_replay)
        if sentinel.read_text(encoding="utf-8") != "todo22-preserve-me\n":
            raise HomologationError("sentinel_mutated_on_replay")
        assert_activation_journal_absent("replay")

        candidate, candidate_sha = make_failing_candidate(repo_root, temp_root)
        failure = _run_installer(candidate, traces[2], skip_packages=True, timeout=360)
        logs.extend([failure.stdout, failure.stderr])
        expected_failure = (
            f"TODO_GLOBAL_DEBIAN_READY=false release={candidate_sha[:12]}"
        )
        if (
            failure.returncode != 1
            or failure.stdout.strip() != expected_failure
            or failure.stderr.strip()
        ):
            evidence["readiness_control_process_diagnostic"] = installer_failure_diagnostic(
                failure,
                traces[2],
                expected_failure,
            )
            raise HomologationError("readiness_control_not_detected")
        if release_state(release)["release_fingerprint"] != before_replay["release_fingerprint"]:
            raise HomologationError("previous_release_mutated_after_failure")
        if os.readlink(BASE / "current") != before_replay["current_target"]:
            raise HomologationError("previous_current_not_restored")
        if sentinel.read_text(encoding="utf-8") != "todo22-preserve-me\n":
            raise HomologationError("sentinel_mutated_after_failure")
        assert_activation_journal_absent("rollback")
        staging = BASE / "releases" / ".staging"
        if not staging.is_dir() or any(staging.iterdir()):
            raise HomologationError("staging_not_clean")
        candidate_release = BASE / "releases" / candidate_sha[:12]
        if not candidate_release.is_dir() or not service_ready():
            raise HomologationError("previous_service_not_restored")

        trace_texts = [_read_trace(path) for path in traces]
        psql_exec_count = audit_psql_execs(trace_texts, secrets)
        assert_sensitive_values_absent([*trace_texts, *logs], secrets)
        evidence.update(
            {
                "initial_install_ready": True,
                "database_readback": True,
                "database_url_local_only": True,
                "psql_exec_count": psql_exec_count,
                "psql_sql_or_password_in_argv": False,
                "sensitive_values_in_output": False,
                "replay_release_unchanged": True,
                "replay_current_unchanged": True,
                "replay_sentinel_preserved": True,
                "readiness_failure_detected": True,
                "previous_release_preserved": True,
                "previous_current_restored": True,
                "previous_service_restored": True,
                "activation_journal_cleared": True,
                "staging_clean": True,
                "synthetic_credentials_local_only": True,
                "neon_used": False,
                "ready": True,
            }
        )
        return 0
    except BaseException as exc:
        evidence["error"] = (
            str(exc) if isinstance(exc, HomologationError) else f"unexpected_{type(exc).__name__}"
        )
        return 1
    finally:
        remove_candidate(repo_root, candidate, temp_root)
        for trace in traces:
            try:
                trace.unlink()
            except FileNotFoundError:
                pass
        if trace_root is not None:
            try:
                trace_root.rmdir()
            except OSError:
                pass
        write_evidence(evidence_path, evidence)
        print(json.dumps(evidence, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--correlation-id", required=True)
    parser.add_argument("--evidence-path", type=Path)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()
    expected_sha = args.expected_sha.strip().lower()
    correlation_id = args.correlation_id.strip()
    if not CORRELATION_RE.fullmatch(correlation_id):
        raise HomologationError("correlation_id_invalid")
    env = dict(os.environ)
    if args.preflight_only:
        preflight(repo_root, expected_sha, env)
        print("TODO22_ISOLATED_PREFLIGHT_OK")
        return 0
    if args.evidence_path is None:
        raise HomologationError("evidence_path_required")
    return homologate(
        repo_root,
        expected_sha,
        correlation_id,
        args.evidence_path.resolve(),
        env,
    )


if __name__ == "__main__":
    raise SystemExit(main())

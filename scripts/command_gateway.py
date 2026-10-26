#!/usr/bin/env python3
"""Command Gateway local: execução governada, auditável e sem shell intermediário."""

from __future__ import annotations

import argparse
import ctypes
import ntpath
import fnmatch
import hashlib
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence


EXIT_POLICY = 20
EXIT_LOCKED = 21
EXIT_COMMAND = 22
EXIT_STATE_CHANGED = 23
EXIT_TIMEOUT = 24
EXIT_SESSION_REQUIRED = 25
EXIT_UNSAFE_GUI_AUTOMATION = 26

SHELL_META = ("&&", "||", ";", "|", ">", "<")
GIT_DESTRUCTIVE = {
    ("push",),
    ("merge",),
    ("rebase",),
    ("clean", "-f"),
    ("clean", "-fd"),
    ("reset", "--hard"),
}
DOCKER_DESTRUCTIVE = {
    ("system", "prune"),
    ("volume", "rm"),
    ("image", "prune"),
    ("container", "prune"),
}
SECRET_RE = re.compile(
    r"(?i)\b(token|secret|password|passwd|api[_-]?key|authorization)\b\s*[:=]\s*([^\s]+)"
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
TOKEN_RE = re.compile(r"\b(?:gh[pousr]_|glpat-)[A-Za-z0-9_-]{10,}")
POSTGRES_URI_RE = re.compile(r"""(?i)\bpostgres(?:ql)?://[^\s"'\\]+""")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")
UNRESOLVED_PATH_VARIABLE_RE = re.compile(
    r"(?:\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*|%[A-Za-z_][A-Za-z0-9_]*%)"
)
POLICY_LIST_FIELDS = {
    "allowed_roots",
    "denied_roots",
    "denied_segments",
    "denied_names",
    "allowed_executables",
    "blocked_executables",
    "git_untracked_excludes",
    "session_source_roots",
    "governed_branch_updates",
    "allowed_executable_specs",
}
POLICY_PATH_LIST_FIELDS = ("allowed_roots", "denied_roots", "session_source_roots")
POLICY_PATH_SCALAR_FIELDS = ("state_dir", "worktree_root")


class GatewayError(RuntimeError):
    def __init__(self, message: str, exit_code: int = EXIT_POLICY) -> None:
        super().__init__(message)
        self.exit_code = exit_code


# Trusted, self-contained Windows-only management probe.  Keep this logic in
# the Gateway itself: installed Gateway/Session Launcher bundles copy this
# module, not auxiliary scripts.  No import or child-script fallback is used.
WSL_EXACT_FLAGS = ("--list", "--running", "--quiet")
WSL_POLICY_FLAG = "noteri_wsl_running_inventory"
WSL_DISTRO_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
WSL_UBUNTU_PATTERN = re.compile(r"Ubuntu(?:-[0-9]{2}\.[0-9]{2})?\Z", re.IGNORECASE)
WSL_MAX_BYTES = 4096
WSL_MAX_DISTROS = 16


class WslStatusBlocked(ValueError):
    """Constant reason codes only. Never include WSL output or names."""


def wsl_is_command(args: Sequence[str]) -> bool:
    return bool(args and isinstance(args[0], str)
                and ntpath.basename(args[0]).casefold() == "wsl.exe")


def wsl_host_context() -> tuple[str, str]:
    return os.name, socket.gethostname()


def wsl_system_path() -> str:
    """Resolve the actual Win32 system directory, never PATH or a caller path."""
    if os.name != "nt":
        raise WslStatusBlocked("WSL_WINDOWS_REQUIRED")
    try:
        library = ctypes.WinDLL("kernel32", use_last_error=True)
        method = library.GetSystemDirectoryW
        method.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
        method.restype = ctypes.c_uint32
        buffer = ctypes.create_unicode_buffer(32768)
        written = method(buffer, len(buffer))
        if not 3 <= written < len(buffer):
            raise WslStatusBlocked("WSL_SYSTEM_BINARY_UNVERIFIED")
        return ntpath.join(buffer.value, "wsl.exe")
    except (OSError, AttributeError, ValueError, TypeError):
        raise WslStatusBlocked("WSL_SYSTEM_BINARY_UNVERIFIED") from None


def wsl_validate_request(args: Sequence[str], risk: int, policy: dict[str, Any]) -> None:
    """Permit exactly one Windows WSL management query under explicit opt-in."""
    if not wsl_is_command(args):
        raise WslStatusBlocked("WSL_COMMAND_INVALID")
    platform, host = wsl_host_context()
    if (platform != "nt" or host.casefold() != "noteri"
            or policy.get("platform") != "windows"
            or policy.get(WSL_POLICY_FLAG) is not True):
        raise WslStatusBlocked("WSL_PROBE_NOT_AUTHORIZED")
    if risk != 1:
        raise WslStatusBlocked("WSL_PROBE_RISK_INVALID")
    if len(args) != 4 or tuple(args[1:]) != WSL_EXACT_FLAGS:
        raise WslStatusBlocked("WSL_PROBE_ARGUMENTS_DENIED")
    original = args[0]
    approved = wsl_system_path()
    if (not ntpath.isabs(original)
            or ntpath.normcase(ntpath.normpath(original)) !=
               ntpath.normcase(ntpath.normpath(approved))):
        raise WslStatusBlocked("WSL_PROBE_BINARY_UNTRUSTED")


def wsl_decode(output: bytes) -> str:
    if type(output) is not bytes or len(output) > WSL_MAX_BYTES:
        raise WslStatusBlocked("WSL_PROBE_OUTPUT_INVALID")
    try:
        if output.startswith((b"\xff\xfe", b"\xfe\xff")):
            return output.decode("utf-16")
        if b"\x00" in output:
            return output.decode("utf-16le")
        return output.decode("utf-8-sig")
    except UnicodeError:
        raise WslStatusBlocked("WSL_PROBE_OUTPUT_INVALID") from None


def wsl_sanitize_output(returncode: int, stdout: bytes, stderr: bytes) -> dict[str, Any]:
    if type(returncode) is not int or returncode != 0:
        raise WslStatusBlocked("WSL_PROBE_FAILED")
    if type(stderr) is not bytes or len(stderr) > WSL_MAX_BYTES:
        raise WslStatusBlocked("WSL_PROBE_OUTPUT_INVALID")
    if wsl_decode(stderr).strip():
        raise WslStatusBlocked("WSL_PROBE_STDERR_UNEXPECTED")
    lines = [value.strip() for value in wsl_decode(stdout).splitlines() if value.strip()]
    if len(lines) > WSL_MAX_DISTROS or any(
        WSL_DISTRO_NAME_PATTERN.fullmatch(name) is None for name in lines
    ) or len(set(name.casefold() for name in lines)) != len(lines):
        raise WslStatusBlocked("WSL_PROBE_OUTPUT_INVALID")
    ubuntu_count = sum(WSL_UBUNTU_PATTERN.fullmatch(name) is not None for name in lines)
    return {
        "result": "NOTERI_WSL_RUNNING_STATUS_READONLY",
        "scope": "current_windows_token_running_distributions",
        "running_count": len(lines),
        "ubuntu_running_candidate": ubuntu_count == 1,
        "ubuntu_running_count": ubuntu_count,
        "distro_names_logged": False,
        "linux_command_executed": False,
        "linux_service_started": False,
        "gateway_posix_verified": False,
        "systemd_verified": False,
        "postgresql_service_verified": False,
        "postgresql_tls_verified": False,
        "authority_ready": False,
        "ready_for_pr": False,
    }


def wsl_run_fixed(
    args: Sequence[str], risk: int, policy: dict[str, Any], *,
    cwd: str, timeout: int, spawn_options: dict[str, Any] | None = None,
    runner=None,
) -> subprocess.CompletedProcess[str]:
    """Called ONLY inside Gateway's session enforcement, repo lock and SHA gate."""
    wsl_validate_request(args, risk, policy)
    method = subprocess.run if runner is None else runner
    try:
        outcome = method(
            list(args), cwd=cwd, capture_output=True, check=False,
            shell=False, timeout=min(timeout, 12),
            **(spawn_options or {}),
        )
        observed = wsl_sanitize_output(outcome.returncode, outcome.stdout, outcome.stderr)
        return subprocess.CompletedProcess(
            list(args), 0, json.dumps(observed, sort_keys=True), "",
        )
    except (WslStatusBlocked, OSError, subprocess.TimeoutExpired, ValueError, TypeError) as exc:
        reason = str(exc) if isinstance(exc, WslStatusBlocked) else "WSL_PROBE_UNAVAILABLE"
        if reason not in {
            "WSL_PROBE_FAILED", "WSL_PROBE_OUTPUT_INVALID",
            "WSL_PROBE_STDERR_UNEXPECTED", "WSL_PROBE_UNAVAILABLE",
        }:
            reason = "WSL_PROBE_UNAVAILABLE"
        return subprocess.CompletedProcess(
            list(args), 22,
            json.dumps({"result": "NOTERI_WSL_RUNNING_STATUS_BLOCKED",
                        "reason": reason, "ready_for_pr": False},
                       sort_keys=True), "",
        )


@dataclass(frozen=True)
class GitState:
    repo_root: str
    branch: str
    head: str
    status_digest: str
    status_count: int


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact(value: str) -> str:
    value = POSTGRES_URI_RE.sub("[REDACTED_POSTGRES_URI]", value)
    value = SECRET_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", value)
    value = BEARER_RE.sub("Bearer [REDACTED]", value)
    return TOKEN_RE.sub("[REDACTED_TOKEN]", value)


def path_variable_names(value: str) -> set[str]:
    names: set[str] = set()
    for match in UNRESOLVED_PATH_VARIABLE_RE.finditer(value):
        token = match.group(0)
        if token.startswith("${"):
            name = token[2:-1]
        elif token.startswith("$"):
            name = token[1:]
        else:
            name = token[1:-1]
        names.add(name)
    return names


def expand_path(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GatewayError("caminho configurado deve ser string não vazia")
    for name in path_variable_names(value):
        if not os.environ.get(name, "").strip():
            raise GatewayError(f"variável de caminho ausente ou vazia: {name}")
    expanded = os.path.expandvars(os.path.expanduser(value))
    if UNRESOLVED_PATH_VARIABLE_RE.search(expanded) or "$" in expanded or "%" in expanded:
        raise GatewayError("variável de ambiente não resolvida em caminho configurado")
    return expanded


def norm(value: str | os.PathLike[str]) -> str:
    expanded = expand_path(os.fspath(value))
    return os.path.normcase(os.path.realpath(os.path.abspath(expanded))).replace("\\", "/")


def lexical_norm(value: str | os.PathLike[str]) -> str:
    expanded = expand_path(os.fspath(value))
    return os.path.normcase(os.path.abspath(expanded)).replace("\\", "/")


def pattern_match(path: str, pattern: str) -> bool:
    p = norm(path)
    # Policy paths are canonicalized once during load. Re-resolving a policy
    # pattern here would let a symlink retarget its authority after validation.
    q = lexical_norm(pattern)
    if any(ch in q for ch in "*?[]"):
        return fnmatch.fnmatchcase(p, q)
    return p == q or p.startswith(q.rstrip("/") + "/")


def is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction and isjunction(path))


def assert_no_link_components(path: Path, *, field: str) -> None:
    if not path.is_absolute():
        raise GatewayError(f"{field} deve ser caminho absoluto")
    parts = path.parts
    current = Path(path.anchor)
    start = 1 if path.anchor else 0
    for part in parts[start:]:
        current /= part
        if not os.path.lexists(current):
            break
        if is_link_or_junction(current):
            raise GatewayError(f"{field} não pode conter symlink ou junction: {current}")


def canonicalize_policy_path(value: str, *, field: str, allow_glob: bool) -> str:
    expanded = expand_path(value)
    candidate = Path(expanded)
    if not candidate.is_absolute():
        raise GatewayError(f"{field} deve ser caminho absoluto")
    if ".." in candidate.parts:
        raise GatewayError(f"{field} não pode conter travessia de diretório")
    wildcard_index = next(
        (index for index, part in enumerate(candidate.parts) if any(ch in part for ch in "*?[]")),
        None,
    )
    if wildcard_index is not None and not allow_glob:
        raise GatewayError(f"{field} não pode conter glob")
    static_parts = candidate.parts if wildcard_index is None else candidate.parts[:wildcard_index]
    suffix_parts = () if wildcard_index is None else candidate.parts[wildcard_index:]
    static_path = Path(*static_parts)
    assert_no_link_components(static_path, field=field)
    canonical_static = static_path.resolve(strict=False)
    if lexical_norm(canonical_static) != lexical_norm(static_path):
        if current_platform() != "windows":
            raise GatewayError(f"{field} mudou ou contém redirecionamento de caminho")
        # Windows may expand an 8.3 component (for example RUNNER~1) while
        # resolving an otherwise direct path.  That spelling change is not a
        # reparse-point redirect.  Re-check both spellings after resolution so
        # symlinks and junctions remain fail-closed, then freeze the canonical
        # long spelling returned by the filesystem.
        assert_no_link_components(static_path, field=field)
        assert_no_link_components(canonical_static, field=field)
    return str(canonical_static.joinpath(*suffix_parts))


def canonical_existing_directory(path: Path) -> Path:
    expanded = Path(expand_path(os.fspath(path)))
    if not expanded.is_absolute():
        raise GatewayError("cwd deve ser caminho absoluto")
    try:
        resolved = expanded.resolve(strict=True)
    except OSError as exc:
        raise GatewayError("cwd não existe ou não pode ser resolvido") from exc
    if not resolved.is_dir():
        raise GatewayError("cwd não é diretório")
    return resolved


@contextmanager
def anchored_subprocess_cwd(path: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    """Keep a POSIX subprocess bound to the directory inode that was validated."""
    canonical = canonical_existing_directory(path)
    if os.name != "posix":
        yield str(canonical), {}
        return

    flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0)) | int(getattr(os, "O_CLOEXEC", 0))
    before = os.stat(canonical, follow_symlinks=False)
    descriptor = os.open(str(canonical), flags)
    try:
        opened = os.fstat(descriptor)
        current = os.stat(canonical, follow_symlinks=False)
        expected_identity = (opened.st_dev, opened.st_ino)
        if not stat.S_ISDIR(opened.st_mode) or expected_identity != (
            before.st_dev,
            before.st_ino,
        ) or expected_identity != (current.st_dev, current.st_ino):
            raise GatewayError(
                "cwd mudou durante a ancoragem; possível concorrência",
                EXIT_STATE_CHANGED,
            )

        anchored: str | None = None
        for descriptor_root in (Path("/proc/self/fd"), Path("/dev/fd")):
            candidate = descriptor_root / str(descriptor)
            try:
                candidate_state = os.stat(candidate)
            except OSError:
                continue
            if (candidate_state.st_dev, candidate_state.st_ino) == expected_identity:
                anchored = str(candidate)
                break
        if anchored is None:
            raise GatewayError("executor POSIX não oferece cwd ancorado por descritor")
        yield anchored, {"pass_fds": (descriptor,)}
    finally:
        os.close(descriptor)


def current_platform() -> str:
    return "windows" if os.name == "nt" else "posix"


def validate_policy(data: object, *, enforce_platform: bool = True) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise GatewayError("política deve ser objeto JSON")
    if type(data.get("version")) is not int or data.get("version") != 1:
        raise GatewayError("versão de política não suportada")
    platform = data.get("platform")
    if platform is not None and platform not in {"windows", "posix"}:
        raise GatewayError("plataforma de política inválida")
    if enforce_platform and platform is not None and platform != current_platform():
        raise GatewayError(
            f"política destinada a {platform}; executor atual é {current_platform()}"
        )
    for field in POLICY_LIST_FIELDS:
        if field not in data:
            continue
        value = data[field]
        if not isinstance(value, list):
            raise GatewayError(f"{field} deve ser lista")
        if field in {"governed_branch_updates", "allowed_executable_specs"}:
            path_keys = (
                ("script", "repo_root")
                if field == "governed_branch_updates"
                else ("path",)
            )
            for index, record in enumerate(value):
                if not isinstance(record, dict):
                    raise GatewayError(f"{field}[{index}] deve ser objeto")
                for key in path_keys:
                    if key in record and (
                        not isinstance(record[key], str) or not record[key].strip()
                    ):
                        raise GatewayError(f"{field}[{index}].{key} deve ser caminho não vazio")
            continue
        for index, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                raise GatewayError(f"{field}[{index}] deve ser string não vazia")
    for field in ("state_dir", "worktree_root", "worktree_prefix", "rules_version"):
        if field in data and (not isinstance(data[field], str) or not data[field].strip()):
            raise GatewayError(f"{field} deve ser string não vazia")
    if ("noteri_wsl_running_inventory" in data
            and type(data["noteri_wsl_running_inventory"]) is not bool):
        raise GatewayError("noteri_wsl_running_inventory deve ser booleano")
    required_environment = data.get("required_path_environment")
    if required_environment is not None:
        if not isinstance(required_environment, dict):
            raise GatewayError("required_path_environment deve ser objeto")
        for name, value in required_environment.items():
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise GatewayError("required_path_environment possui nome inválido")
            if not isinstance(value, str) or not value.strip():
                raise GatewayError(
                    f"required_path_environment.{name} deve ser caminho não vazio"
                )
    return data


def configured_policy_paths(policy: dict[str, Any]) -> list[tuple[str, str]]:
    configured: list[tuple[str, str]] = []
    for field in POLICY_PATH_LIST_FIELDS:
        for index, value in enumerate(policy.get(field, [])):
            configured.append((f"{field}[{index}]", value))
    for field in POLICY_PATH_SCALAR_FIELDS:
        value = policy.get(field)
        if value is not None:
            configured.append((field, value))
    for index, record in enumerate(policy.get("governed_branch_updates", [])):
        for key in ("script", "repo_root"):
            value = record.get(key)
            if value is not None:
                configured.append((f"governed_branch_updates[{index}].{key}", value))
    for index, record in enumerate(policy.get("allowed_executable_specs", [])):
        value = record.get("path")
        if value is not None:
            configured.append((f"allowed_executable_specs[{index}].path", value))
    return configured


def validate_required_path_environment(policy: dict[str, Any]) -> None:
    required = policy.get("required_path_environment")
    if required is None:
        return
    referenced: set[str] = set()
    for _, value in configured_policy_paths(policy):
        referenced.update(path_variable_names(value))
    required_names = set(required)
    if referenced != required_names:
        missing = sorted(referenced - required_names)
        unused = sorted(required_names - referenced)
        details: list[str] = []
        if missing:
            details.append("sem vínculo=" + ",".join(missing))
        if unused:
            details.append("não referenciada=" + ",".join(unused))
        raise GatewayError(
            "required_path_environment diverge das variáveis de caminho"
            + (": " + "; ".join(details) if details else "")
        )
    for name, trusted_value in required.items():
        if path_variable_names(trusted_value) or "$" in trusted_value or "%" in trusted_value:
            raise GatewayError(
                f"required_path_environment.{name} deve usar caminho literal"
            )
        if not Path(trusted_value).is_absolute():
            raise GatewayError(
                f"required_path_environment.{name} deve ser caminho absoluto"
            )
        actual_value = os.environ.get(name, "")
        if not actual_value.strip():
            raise GatewayError(f"variável de caminho ausente ou vazia: {name}")
        if path_variable_names(actual_value) or "$" in actual_value or "%" in actual_value:
            raise GatewayError(f"variável de caminho deve usar valor literal: {name}")
        if not Path(actual_value).is_absolute():
            raise GatewayError(f"variável de caminho deve ser absoluta: {name}")
        actual_lexical = os.path.normcase(os.path.abspath(actual_value))
        trusted_lexical = os.path.normcase(os.path.abspath(trusted_value))
        if actual_lexical != trusted_lexical:
            raise GatewayError(
                f"variável de caminho diverge do valor confiável: {name}"
            )


def resolve_runtime_policy_paths(policy: dict[str, Any]) -> dict[str, Any]:
    resolved = dict(policy)
    for field in POLICY_PATH_LIST_FIELDS:
        if field in policy:
            resolved[field] = [
                canonicalize_policy_path(
                    value,
                    field=f"{field}[{index}]",
                    allow_glob=True,
                )
                for index, value in enumerate(policy[field])
            ]
    for field in POLICY_PATH_SCALAR_FIELDS:
        value = policy.get(field)
        if value is not None:
            resolved[field] = canonicalize_policy_path(
                value,
                field=field,
                allow_glob=False,
            )
    governed_updates: list[dict[str, Any]] = []
    for index, record in enumerate(policy.get("governed_branch_updates", [])):
        resolved_record = dict(record)
        for key in ("script", "repo_root"):
            value = record.get(key)
            if value is not None:
                resolved_record[key] = canonicalize_policy_path(
                    value,
                    field=f"governed_branch_updates[{index}].{key}",
                    allow_glob=False,
                )
        governed_updates.append(resolved_record)
    if "governed_branch_updates" in policy:
        resolved["governed_branch_updates"] = governed_updates
    executable_specs: list[dict[str, Any]] = []
    for index, record in enumerate(policy.get("allowed_executable_specs", [])):
        resolved_record = dict(record)
        value = record.get("path")
        if value is not None:
            resolved_record["path"] = canonicalize_policy_path(
                value,
                field=f"allowed_executable_specs[{index}].path",
                allow_glob=False,
            )
        executable_specs.append(resolved_record)
    if "allowed_executable_specs" in policy:
        resolved["allowed_executable_specs"] = executable_specs
    return resolved


def validate_runtime_policy_paths(policy: dict[str, Any]) -> None:
    for field in POLICY_PATH_LIST_FIELDS:
        for index, configured in enumerate(policy.get(field, [])):
            expanded = expand_path(configured)
            if not Path(expanded).is_absolute():
                raise GatewayError(f"{field}[{index}] deve ser caminho absoluto")
    for field in ("state_dir", "worktree_root"):
        configured = policy.get(field)
        if configured is None:
            continue
        expanded = expand_path(configured)
        if not Path(expanded).is_absolute():
            raise GatewayError(f"{field} deve ser caminho absoluto")
    worktree_root = policy.get("worktree_root")
    if worktree_root and policy.get("allowed_roots"):
        probe = Path(expand_path(worktree_root)) / "__policy_probe__"
        if not any(pattern_match(str(probe), item) for item in policy["allowed_roots"]):
            raise GatewayError("worktree_root não está coberto por allowed_roots")


def load_policy(
    path: Path,
    *,
    enforce_platform: bool = True,
    validate_paths: bool = True,
) -> dict[str, Any]:
    data = validate_policy(
        json.loads(path.read_text(encoding="utf-8")),
        enforce_platform=enforce_platform,
    )
    if validate_paths:
        validate_required_path_environment(data)
        data = resolve_runtime_policy_paths(data)
        validate_runtime_policy_paths(data)
    return data


def assert_allowed_path(cwd: Path, policy: dict[str, Any]) -> None:
    cwd_norm = norm(cwd)
    if any(pattern_match(cwd_norm, item) for item in policy.get("denied_roots", [])):
        raise GatewayError("diretório bloqueado pela política")
    if not any(pattern_match(cwd_norm, item) for item in policy.get("allowed_roots", [])):
        raise GatewayError("diretório fora da allowlist")
    parts = {part.casefold() for part in Path(cwd_norm).parts}
    denied = {item.casefold() for item in policy.get("denied_segments", [])}
    if parts & denied:
        raise GatewayError("segmento sensível bloqueado pela política")


def sensitive_reference(args: Sequence[str], policy: dict[str, Any]) -> bool:
    denied_names = policy.get("denied_names", [])
    denied_segments = {item.casefold() for item in policy.get("denied_segments", [])}
    for raw in args:
        token = raw.replace("\\", "/")
        if SECRET_RE.search(raw) or BEARER_RE.search(raw) or TOKEN_RE.search(raw) or POSTGRES_URI_RE.search(raw):
            return True
        parts = [part for part in token.split("/") if part]
        if any(part.casefold() in denied_segments for part in parts):
            return True
        basename = parts[-1] if parts else token
        if any(fnmatch.fnmatchcase(basename.casefold(), pat.casefold()) for pat in denied_names):
            return True
    return False


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_executable_spec(
    spec: object,
    *,
    expected_executable: str | None = None,
    require_exists: bool = True,
) -> tuple[Path, str]:
    """Validate one exact executable authorization without broad name allowlisting."""
    if not isinstance(spec, dict):
        raise GatewayError("especificação de executável inválida")
    configured = spec.get("path")
    expected_sha = spec.get("sha256")
    if not isinstance(configured, str) or not configured.strip():
        raise GatewayError("especificação de executável sem caminho")
    candidate = Path(expand_path(configured))
    if not candidate.is_absolute():
        raise GatewayError("caminho aprovado de executável deve ser absoluto")
    if not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha):
        raise GatewayError("hash aprovado de executável inválido")
    name = candidate.name.casefold().removesuffix(".exe")
    if expected_executable is not None and name != expected_executable.casefold().removesuffix(".exe"):
        raise GatewayError("nome do executável aprovado não corresponde ao esperado")
    if require_exists:
        try:
            candidate = candidate.resolve(strict=True)
        except OSError as exc:
            raise GatewayError(f"caminho de executável inválido: {name}") from exc
    return candidate, expected_sha.casefold()


def assert_allowed_executable(raw: str, policy: dict[str, Any]) -> str:
    exe = Path(raw).name.casefold()
    if exe.endswith(".exe"):
        exe = exe[:-4]
    blocked = {item.casefold() for item in policy.get("blocked_executables", [])}
    if exe in blocked:
        raise GatewayError(f"executável não autorizado: {exe}")
    allowed = {item.casefold() for item in policy.get("allowed_executables", [])}
    if exe in allowed:
        return exe

    candidate = Path(expand_path(raw))
    if not candidate.is_absolute():
        raise GatewayError(f"executável não autorizado: {exe}")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise GatewayError(f"caminho de executável inválido: {exe}") from exc

    matched_path = False
    for spec in policy.get("allowed_executable_specs", []):
        try:
            approved, expected_sha = validate_executable_spec(spec)
        except GatewayError:
            continue
        if norm(resolved) != norm(approved):
            continue
        matched_path = True
        if sha256_file(resolved).casefold() != expected_sha:
            raise GatewayError(f"integridade do executável não comprovada: {exe}")
        return exe
    if matched_path:
        raise GatewayError(f"integridade do executável não comprovada: {exe}")
    raise GatewayError(f"executável não autorizado: {exe}")


def validate_command(args: Sequence[str], risk: int, policy: dict[str, Any], cwd: Path | None = None) -> None:
    if risk not in (1, 2):
        raise GatewayError("risco 3 não é executado automaticamente")
    if not args:
        raise GatewayError("comando ausente")
    if sensitive_reference(args, policy):
        raise GatewayError("referência potencialmente sensível bloqueada")
    # Explicitly reviewed read-only management query, NOT a general WSL bypass.
    # Every other wsl.exe invocation remains blocked by blocked_executables.
    if wsl_is_command(args):
        try:
            wsl_validate_request(args, risk, policy)
        except WslStatusBlocked as exc:
            raise GatewayError(str(exc)) from None
        return
    exe = assert_allowed_executable(args[0], policy)
    if any(meta in arg for arg in args for meta in SHELL_META):
        raise GatewayError("composição/redirecionamento de shell bloqueado")
    if policy.get("deny_inline_code", True):
        if exe in {"python", "python3"} and any(arg in {"-c", "-m"} for arg in args[1:]):
            if "-c" in args[1:]:
                raise GatewayError("código Python inline bloqueado")
        if exe == "node" and any(arg in {"-e", "--eval", "-p", "--print"} for arg in args[1:]):
            raise GatewayError("código Node inline bloqueado")

    tail = tuple(arg.casefold() for arg in args[1:])
    if exe == "git" and any(tail[: len(p)] == p for p in GIT_DESTRUCTIVE):
        raise GatewayError("operação Git crítica/destrutiva bloqueada")
    if exe == "docker" and any(tail[: len(p)] == p for p in DOCKER_DESTRUCTIVE):
        raise GatewayError("operação Docker destrutiva bloqueada")
    validate_governed_branch_update(args, cwd or Path.cwd(), policy)


def python_script_path(args: Sequence[str], cwd: Path) -> Path | None:
    exe = Path(args[0]).name.casefold()
    if exe.endswith(".exe"):
        exe = exe[:-4]
    if exe not in {"python", "python3"}:
        return None
    for arg in args[1:]:
        if arg in {"-c", "-m"}:
            return None
        if arg == "--" or arg.startswith("-"):
            continue
        candidate = Path(arg)
        if candidate.suffix.casefold() != ".py":
            return None
        return candidate if candidate.is_absolute() else cwd / candidate
    return None


def validate_governed_branch_update(args: Sequence[str], cwd: Path, policy: dict[str, Any]) -> None:
    """Allow only one exact reviewed maintenance record for this invocation."""
    exe = Path(args[0]).name.casefold() if args else ""
    if exe.endswith(".exe"):
        exe = exe[:-4]
    script = python_script_path(args, cwd)
    if exe not in {"python", "python3"} or script is None:
        return
    if script.name != "update_branch_fast_forward_governed.py":
        return
    pairs: dict[str, str] = {}
    idx = 2
    allowed = {"--repo", "--remote", "--branch", "--expected-remote", "--expected-head", "--correlation-id"}
    while idx < len(args):
        key = args[idx]
        if key not in allowed or idx + 1 >= len(args):
            raise GatewayError("governed branch update arguments are restricted")
        pairs[key] = args[idx + 1]
        idx += 2
    required = allowed
    if set(pairs) != required:
        raise GatewayError("governed branch update requires the complete reviewed argument set")
    if any(token.casefold() in {"--force", "-f", "--force-with-lease"} for token in args):
        raise GatewayError("force push is forbidden for governed branch maintenance")

    records = policy.get("governed_branch_updates", [])
    matches = []
    for record in records:
        frozen_script = Path(str(record.get("script", "")))
        frozen_repo = str(record.get("repo_root", ""))
        if lexical_norm(script) != lexical_norm(frozen_script):
            continue
        if lexical_norm(cwd) != lexical_norm(frozen_repo):
            continue
        if lexical_norm(pairs["--repo"]) != lexical_norm(frozen_repo):
            continue
        if any(
            pairs[key].casefold() != str(record.get(record_key, "")).casefold()
            for key, record_key in (
                ("--remote", "remote"),
                ("--branch", "branch"),
                ("--expected-remote", "expected_remote"),
                ("--expected-head", "expected_head"),
            )
        ):
            continue
        matches.append(record)
    if len(matches) != 1:
        raise GatewayError("governed branch update capability is not configured or is ambiguous")

    frozen_script = Path(str(matches[0]["script"]))
    assert_no_link_components(frozen_script, field="governed branch update script")
    try:
        resolved_script = frozen_script.resolve(strict=True)
    except OSError as exc:
        raise GatewayError("governed branch update script is unavailable") from exc
    if lexical_norm(resolved_script) != lexical_norm(frozen_script):
        raise GatewayError("governed branch update script changed after policy validation")


def assert_safe_gui_automation(args: Sequence[str], cwd: Path, policy: dict[str, Any]) -> None:
    if not policy.get("block_gui_input_automation", True):
        return
    script = python_script_path(args, cwd)
    if script is None or not script.is_file():
        return
    content = script.read_text(encoding="utf-8", errors="replace").casefold()
    always = tuple(item.casefold() for item in policy.get("blocked_gui_input_markers", [
        "keybd_event(", "mouse_event(", "pyautogui.write(", "pyautogui.press(",
        "keyboard.write(", "keyboard.press(", "setclipboardtext("
    ]))
    contextual = tuple(item.casefold() for item in policy.get("blocked_gui_context_markers", [
        ".invoke(", ".type_keys(", ".click_input(", "send_keys(", "win32clipboard"
    ]))
    unsafe = any(item in content for item in always)
    unsafe = unsafe or ("pywinauto" in content and any(item in content for item in contextual))
    if unsafe:
        raise GatewayError(
            "automacao de entrada GUI bloqueada; use transporte nativo governado e validacao semantica",
            EXIT_UNSAFE_GUI_AUTOMATION,
        )


def run_capture(args: Sequence[str], cwd: Path, timeout: int = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args), cwd=str(cwd), text=True, capture_output=True,
        encoding="utf-8", errors="replace", shell=False, timeout=timeout, check=False,
    )


def git_state(cwd: Path, require_repo: bool = True, policy: dict[str, Any] | None = None) -> GitState | None:
    state_timeout = int((policy or {}).get("git_state_timeout_seconds", 15))
    if state_timeout < 5 or state_timeout > 120:
        raise GatewayError("git_state_timeout_seconds deve estar entre 5 e 120s")
    root = run_capture(["git", "rev-parse", "--show-toplevel"], cwd, timeout=state_timeout)
    if root.returncode != 0:
        if require_repo:
            raise GatewayError("diretório não é repositório Git")
        return None
    if root.stderr.strip():
        raise GatewayError("estado Git incompleto: rev-parse produziu aviso/erro em stderr", EXIT_STATE_CHANGED)
    repo_root = Path(root.stdout.strip())
    head = run_capture(["git", "rev-parse", "HEAD"], repo_root, timeout=state_timeout)
    branch = run_capture(["git", "branch", "--show-current"], repo_root, timeout=state_timeout)
    tracked = run_capture(["git", "status", "--porcelain=v1", "-z", "--untracked-files=no"], repo_root, timeout=state_timeout)
    index = run_capture(["git", "ls-files", "-s", "-z"], repo_root, timeout=state_timeout)
    pathspec = ["."] + [f":(exclude){item}" for item in (policy or {}).get("git_untracked_excludes", [])]
    untracked = run_capture(["git", "ls-files", "--others", "--exclude-standard", "-z", "--", *pathspec], repo_root, timeout=state_timeout)
    checks = (head, branch, tracked, index, untracked)
    modified = run_capture(["git", "ls-files", "-m", "-d", "-z"], repo_root, timeout=state_timeout)
    checks = (head, branch, tracked, index, modified, untracked)
    if any(item.returncode != 0 for item in checks):
        raise GatewayError("não foi possível obter estado Git")
    if any(item.stderr.strip() for item in checks):
        raise GatewayError("estado Git incompleto: comando Git produziu aviso/erro em stderr", EXIT_STATE_CHANGED)

    def add_path_content(digest: Any, rel: str) -> None:
        candidate = repo_root / rel
        try:
            if candidate.is_symlink():
                payload = ("SYMLINK\0" + os.readlink(candidate)).encode("utf-8", errors="surrogateescape")
            elif candidate.is_file():
                file_hash = hashlib.sha256()
                with candidate.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        file_hash.update(chunk)
                payload = ("FILE\0" + file_hash.hexdigest()).encode("ascii")
            elif candidate.exists():
                payload = b"DIR"
            else:
                payload = b"MISSING"
        except OSError as exc:
            raise GatewayError(f"estado Git incompleto: não foi possível ler {rel}", EXIT_STATE_CHANGED) from exc
        digest.update(rel.encode("utf-8", errors="surrogateescape") + b"\0" + payload + b"\0")

    digest = hashlib.sha256()
    digest.update(("T\0" + tracked.stdout + "I\0" + index.stdout + "M\0" + modified.stdout + "U\0" + untracked.stdout).encode("utf-8", errors="replace"))
    modified_paths = [item for item in modified.stdout.split("\x00") if item]
    for rel in sorted(set(modified_paths)):
        add_path_content(digest, rel)
    untracked_paths = [item for item in untracked.stdout.split("\x00") if item]
    for rel in sorted(untracked_paths):
        add_path_content(digest, rel)
    count = len([x for x in tracked.stdout.split("\x00") if x]) + len(untracked_paths)
    return GitState(repo_root=str(repo_root), branch=branch.stdout.strip() or "DETACHED", head=head.stdout.strip(), status_digest=digest.hexdigest(), status_count=count)


def tracked_case_collisions(cwd: Path) -> list[tuple[str, str]]:
    result = run_capture(["git", "ls-files", "-z"], cwd)
    if result.returncode != 0 or result.stderr.strip():
        raise GatewayError("não foi possível validar colisões de caminhos rastreados", EXIT_STATE_CHANGED)
    seen: dict[str, str] = {}
    collisions: list[tuple[str, str]] = []
    for path in (item for item in result.stdout.split("\x00") if item):
        key = path.casefold()
        previous = seen.get(key)
        if previous is not None and previous != path:
            collisions.append((previous, path))
        else:
            seen[key] = path
    return collisions


def state_key(state: GitState) -> str:
    return hashlib.sha256(norm(state.repo_root).encode("utf-8")).hexdigest()[:24]


def _uses_posix_state_dir_fd() -> bool:
    """Return whether state I/O must use descriptor-relative POSIX primitives."""
    if os.name != "posix":
        return False
    required_flags = ("O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required_flags):
        raise GatewayError("plataforma POSIX sem suporte a diretórios de estado ancorados")
    supported_dir_fd_names = {
        getattr(function, "__name__", "") for function in os.supports_dir_fd
    }
    required_dir_fd_names = {"open", "mkdir", "stat", "unlink"}
    if not required_dir_fd_names.issubset(supported_dir_fd_names):
        raise GatewayError("plataforma POSIX sem suporte completo a dir_fd para o estado")
    return True


def _state_directory_flags() -> int:
    return (
        os.O_RDONLY
        | int(getattr(os, "O_DIRECTORY", 0))
        | int(getattr(os, "O_NOFOLLOW", 0))
        | int(getattr(os, "O_CLOEXEC", 0))
    )


def _validate_private_state_directory(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise GatewayError("componente do estado não é diretório")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise GatewayError("diretório de estado pertence a outra identidade")
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise GatewayError("diretório de estado não pode permitir escrita de grupo/outros")


def _open_child_state_directory(parent_fd: int, component: str, *, create: bool) -> int:
    component = validate_state_component(component)
    try:
        child_fd = os.open(component, _state_directory_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise
        try:
            os.mkdir(component, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        child_fd = os.open(component, _state_directory_flags(), dir_fd=parent_fd)
    metadata = os.fstat(child_fd)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(child_fd)
        raise GatewayError("componente do estado não é diretório")
    return child_fd


def _open_absolute_state_directory(path: Path, *, create: bool) -> int:
    if not path.is_absolute() or not path.anchor:
        raise GatewayError("state_dir deve ser caminho absoluto")
    descriptor = os.open(path.anchor, _state_directory_flags())
    try:
        for component in path.parts[1:]:
            child = _open_child_state_directory(descriptor, component, create=create)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def state_dir(policy: dict[str, Any]) -> Path:
    configured_value = policy.get("state_dir")
    configured = canonicalize_policy_path(
        configured_value
        if configured_value is not None
        else str(Path.home() / ".reqsys-command-gateway"),
        field="state_dir",
        allow_glob=False,
    )
    path = Path(configured)
    if not path.is_absolute():
        raise GatewayError("state_dir deve ser caminho absoluto")
    if _uses_posix_state_dir_fd():
        descriptor = _open_absolute_state_directory(path, create=True)
        try:
            _validate_private_state_directory(descriptor)
        finally:
            os.close(descriptor)
        return path

    # Windows fallback: Python does not expose openat-style directory handles.
    # These checks fail closed for stable symlinks/junctions. The state root must
    # also be protected by ACL; a same-identity process can otherwise race a
    # pathname check and is explicitly outside this fallback's guarantee.
    assert_no_link_components(path, field="state_dir")
    path.mkdir(parents=True, exist_ok=True)
    assert_no_link_components(path, field="state_dir")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise GatewayError("state_dir não pode ser resolvido") from exc
    if lexical_norm(resolved) != lexical_norm(path):
        raise GatewayError("state_dir mudou ou contém redirecionamento de caminho")
    return resolved


def validate_state_component(component: str) -> str:
    if (
        not isinstance(component, str)
        or not component
        or component in {".", ".."}
        or "/" in component
        or "\\" in component
    ):
        raise GatewayError("componente de caminho do estado inválido")
    return component


def _state_relative_parts(policy: dict[str, Any], path: Path) -> tuple[Path, Path, tuple[str, ...]]:
    root = state_dir(policy)
    candidate = Path(path)
    if not candidate.is_absolute():
        raise GatewayError("caminho de estado deve ser absoluto")
    root_lexical = lexical_norm(root)
    candidate_lexical = lexical_norm(candidate)
    try:
        common = os.path.commonpath([root_lexical, candidate_lexical])
    except ValueError as exc:
        raise GatewayError("caminho de estado fora da raiz canônica") from exc
    if os.path.normcase(common) != os.path.normcase(root_lexical):
        raise GatewayError("caminho de estado fora da raiz canônica")
    relative = os.path.relpath(candidate_lexical, root_lexical)
    if relative == os.curdir:
        parts: tuple[str, ...] = ()
    else:
        parts = tuple(validate_state_component(item) for item in Path(relative).parts)
    normalized = root.joinpath(*parts)
    return root, normalized, parts


def assert_secure_state_path(policy: dict[str, Any], path: Path) -> Path:
    _, candidate, _ = _state_relative_parts(policy, path)
    candidate_lexical = lexical_norm(candidate)
    assert_no_link_components(candidate, field="caminho de estado")
    resolved = candidate.resolve(strict=False)
    if lexical_norm(resolved) != candidate_lexical:
        raise GatewayError("caminho de estado mudou ou contém redirecionamento")
    return candidate


@contextmanager
def _open_state_directory_fd(
    policy: dict[str, Any],
    components: Sequence[str] = (),
    *,
    create: bool,
) -> Iterator[int]:
    if not _uses_posix_state_dir_fd():
        raise GatewayError("dir_fd de estado indisponível nesta plataforma")
    root = state_dir(policy)
    descriptor = _open_absolute_state_directory(root, create=False)
    try:
        _validate_private_state_directory(descriptor)
        for raw_component in components:
            child = _open_child_state_directory(
                descriptor,
                validate_state_component(raw_component),
                create=create,
            )
            try:
                _validate_private_state_directory(child)
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        yield descriptor
        try:
            verification_fd = _open_absolute_state_directory(root, create=False)
            try:
                _validate_private_state_directory(verification_fd)
                for raw_component in components:
                    child = _open_child_state_directory(
                        verification_fd,
                        validate_state_component(raw_component),
                        create=False,
                    )
                    try:
                        _validate_private_state_directory(child)
                    except BaseException:
                        os.close(child)
                        raise
                    os.close(verification_fd)
                    verification_fd = child
                expected = os.fstat(descriptor)
                current = os.fstat(verification_fd)
                if (expected.st_dev, expected.st_ino) != (current.st_dev, current.st_ino):
                    raise GatewayError("diretório de estado mudou durante a operação")
            finally:
                os.close(verification_fd)
        except GatewayError:
            raise
        except OSError as exc:
            raise GatewayError("diretório de estado mudou durante a operação") from exc
    finally:
        os.close(descriptor)


def secure_state_directory(policy: dict[str, Any], *components: str) -> Path:
    validated = tuple(validate_state_component(item) for item in components)
    target = state_dir(policy).joinpath(*validated)
    assert_no_link_components(target, field="subdiretório de estado")
    if _uses_posix_state_dir_fd():
        with _open_state_directory_fd(policy, validated, create=True):
            pass
        return target
    current = state_dir(policy)
    for component in validated:
        candidate = assert_secure_state_path(policy, current / component)
        candidate.mkdir(exist_ok=True)
        candidate = assert_secure_state_path(policy, candidate)
        if not candidate.is_dir():
            raise GatewayError("subdiretório de estado inválido")
        current = candidate
    return current


def secure_state_path(
    policy: dict[str, Any],
    *components: str,
    create_parent: bool = False,
) -> Path:
    if not components:
        return state_dir(policy)
    validated = [validate_state_component(item) for item in components]
    if create_parent and len(validated) > 1:
        parent = secure_state_directory(policy, *validated[:-1])
    else:
        parent = state_dir(policy).joinpath(*validated[:-1])
    return assert_secure_state_path(policy, parent / validated[-1])


def _state_open_flags(flags: int) -> int:
    result = flags | int(getattr(os, "O_CLOEXEC", 0)) | int(getattr(os, "O_NOINHERIT", 0))
    if os.name == "posix":
        result |= int(getattr(os, "O_NOFOLLOW", 0))
    return result


StateFileIdentity = tuple[int, int]


def _validate_state_file_metadata(metadata: os.stat_result) -> StateFileIdentity:
    if not stat.S_ISREG(metadata.st_mode):
        raise GatewayError("arquivo de estado deve ser arquivo regular")
    if metadata.st_nlink != 1:
        raise GatewayError("arquivo de estado não pode possuir hardlinks")
    if os.name == "posix" and hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise GatewayError("arquivo de estado pertence a outra identidade")
    return int(metadata.st_dev), int(metadata.st_ino)


def _validate_open_state_file(
    descriptor: int,
    *,
    parent_fd: int | None = None,
    name: str | None = None,
    path: Path | None = None,
) -> StateFileIdentity:
    opened = os.fstat(descriptor)
    identity = _validate_state_file_metadata(opened)
    if parent_fd is not None and name is not None:
        entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    elif path is not None:
        entry = os.stat(path, follow_symlinks=False)
    else:
        raise GatewayError("validação de arquivo de estado sem entrada")
    entry_identity = _validate_state_file_metadata(entry)
    if entry_identity != identity:
        raise GatewayError("arquivo de estado mudou durante a abertura")
    return identity


def _open_state_file(
    policy: dict[str, Any],
    path: Path,
    flags: int,
    mode: int = 0o600,
) -> tuple[int, StateFileIdentity]:
    _, candidate, parts = _state_relative_parts(policy, path)
    if not parts:
        raise GatewayError("raiz do estado não pode ser aberta como arquivo")
    if _uses_posix_state_dir_fd():
        descriptor: int | None = None
        try:
            with _open_state_directory_fd(policy, parts[:-1], create=False) as parent_fd:
                descriptor = os.open(
                    parts[-1],
                    _state_open_flags(flags),
                    mode,
                    dir_fd=parent_fd,
                )
                identity = _validate_open_state_file(
                    descriptor,
                    parent_fd=parent_fd,
                    name=parts[-1],
                )
            return descriptor, identity
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            raise

    candidate = assert_secure_state_path(policy, candidate)
    descriptor = os.open(str(candidate), _state_open_flags(flags), mode)
    try:
        candidate = assert_secure_state_path(policy, candidate)
        identity = _validate_open_state_file(descriptor, path=candidate)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, identity


def read_state_text(policy: dict[str, Any], path: Path) -> str:
    descriptor, _ = _open_state_file(policy, path, os.O_RDONLY)
    with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
        return handle.read()


def write_state_text(
    policy: dict[str, Any],
    path: Path,
    value: str,
    *,
    exclusive: bool = False,
    append: bool = False,
) -> StateFileIdentity:
    flags = os.O_WRONLY | os.O_CREAT
    if exclusive:
        flags |= os.O_EXCL
    elif append:
        flags |= os.O_APPEND
    descriptor, identity = _open_state_file(policy, path, flags)
    if not append:
        os.ftruncate(descriptor, 0)
    with os.fdopen(
        descriptor,
        "a" if append else "w",
        encoding="utf-8",
        newline="\n",
    ) as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    return identity


def _state_entry_metadata(parent_fd: int, name: str) -> os.stat_result:
    metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    _validate_state_file_metadata(metadata)
    return metadata


def unlink_state_file(
    policy: dict[str, Any],
    path: Path,
    *,
    expected_identity: StateFileIdentity | None = None,
) -> None:
    _, candidate, parts = _state_relative_parts(policy, path)
    if not parts:
        raise GatewayError("raiz do estado não pode ser removida")
    if _uses_posix_state_dir_fd():
        with _open_state_directory_fd(policy, parts[:-1], create=False) as parent_fd:
            metadata = _state_entry_metadata(parent_fd, parts[-1])
            identity = (int(metadata.st_dev), int(metadata.st_ino))
            if expected_identity is not None and identity != expected_identity:
                raise GatewayError("arquivo de estado mudou antes da remoção")
            os.unlink(parts[-1], dir_fd=parent_fd)
            os.fsync(parent_fd)
        return
    candidate = assert_secure_state_path(policy, candidate)
    metadata = os.stat(candidate, follow_symlinks=False)
    identity = _validate_state_file_metadata(metadata)
    if expected_identity is not None and identity != expected_identity:
        raise GatewayError("arquivo de estado mudou antes da remoção")
    candidate.unlink()


def _validate_fallback_state_file(policy: dict[str, Any], path: Path) -> StateFileIdentity:
    descriptor, identity = _open_state_file(policy, path, os.O_RDONLY)
    os.close(descriptor)
    return identity


def replace_state_file(policy: dict[str, Any], source: Path, target: Path) -> None:
    _, safe_source, source_parts = _state_relative_parts(policy, source)
    _, safe_target, target_parts = _state_relative_parts(policy, target)
    if not source_parts or not target_parts or source_parts[:-1] != target_parts[:-1]:
        raise GatewayError("substituição de estado exige o mesmo diretório")
    if _uses_posix_state_dir_fd():
        with _open_state_directory_fd(policy, source_parts[:-1], create=False) as parent_fd:
            source_fd = os.open(
                source_parts[-1],
                _state_open_flags(os.O_RDONLY),
                dir_fd=parent_fd,
            )
            try:
                source_identity = _validate_open_state_file(
                    source_fd,
                    parent_fd=parent_fd,
                    name=source_parts[-1],
                )
                try:
                    _state_entry_metadata(parent_fd, target_parts[-1])
                except FileNotFoundError:
                    pass
                os.replace(
                    source_parts[-1],
                    target_parts[-1],
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                replaced = _state_entry_metadata(parent_fd, target_parts[-1])
                replaced_identity = (int(replaced.st_dev), int(replaced.st_ino))
                if replaced_identity != source_identity:
                    raise GatewayError("arquivo de estado mudou durante a substituição")
                os.fsync(parent_fd)
            finally:
                os.close(source_fd)
        return

    safe_source = assert_secure_state_path(policy, safe_source)
    safe_target = assert_secure_state_path(policy, safe_target)
    source_identity = _validate_fallback_state_file(policy, safe_source)
    if safe_target.exists():
        _validate_fallback_state_file(policy, safe_target)
    os.replace(safe_source, safe_target)
    safe_target = assert_secure_state_path(policy, safe_target)
    if _validate_fallback_state_file(policy, safe_target) != source_identity:
        raise GatewayError("arquivo de estado mudou durante a substituição")


def atomic_write_state_text(policy: dict[str, Any], path: Path, value: str) -> None:
    root, target, parts = _state_relative_parts(policy, path)
    if not parts:
        raise GatewayError("raiz do estado não pode receber conteúdo")
    for _ in range(8):
        temporary_name = validate_state_component(
            f".{parts[-1]}.{secrets.token_hex(16)}.tmp"
        )
        temporary = root.joinpath(*parts[:-1], temporary_name)
        try:
            identity = write_state_text(
                policy,
                temporary,
                value,
                exclusive=True,
            )
        except FileExistsError:
            continue
        try:
            replace_state_file(policy, temporary, target)
        except BaseException:
            try:
                unlink_state_file(policy, temporary, expected_identity=identity)
            except (FileNotFoundError, GatewayError, OSError):
                pass
            raise
        return
    raise GatewayError("não foi possível criar arquivo temporário exclusivo do estado")


def snapshot_sha256(payload: dict[str, Any]) -> str:
    body = {key: value for key, value in payload.items() if key != "snapshot_sha256"}
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def validate_session_snapshot(policy: dict[str, Any], session_id: str, reservation: dict[str, Any]) -> dict[str, Any] | None:
    if not policy.get("require_preflight_snapshot", False):
        return None
    path = secure_state_path(policy, "snapshots", f"{session_id}.json")
    if not path.is_file():
        raise GatewayError("BOOTSTRAP_REQUIRED: snapshot de preflight inexistente", EXIT_SESSION_REQUIRED)
    try:
        snapshot = json.loads(read_state_text(policy, path))
    except (OSError, json.JSONDecodeError) as exc:
        raise GatewayError("BOOTSTRAP_REQUIRED: snapshot de preflight inválido", EXIT_SESSION_REQUIRED) from exc
    expected = snapshot.get("snapshot_sha256")
    if not expected or expected != snapshot_sha256(snapshot):
        raise GatewayError("BOOTSTRAP_REQUIRED: integridade do snapshot inválida", EXIT_SESSION_REQUIRED)
    if snapshot.get("session_id") != session_id or snapshot.get("result") != "BOOTSTRAP_OK":
        raise GatewayError("BOOTSTRAP_REQUIRED: snapshot não pertence à sessão", EXIT_SESSION_REQUIRED)
    for key in ("repo_root", "reserved_worktree"):
        if norm(str(snapshot.get(key, ""))) != norm(str(reservation.get(key, ""))):
            raise GatewayError("BOOTSTRAP_REQUIRED: snapshot diverge da reserva", EXIT_SESSION_REQUIRED)
    if snapshot.get("reservation_status") != reservation.get("status"):
        raise GatewayError("BOOTSTRAP_REQUIRED: estado da reserva diverge do snapshot", EXIT_SESSION_REQUIRED)
    return snapshot


def enforce_session(policy: dict[str, Any], session_id: str | None, cwd: Path, risk: int | None = None) -> dict[str, Any]:
    if not policy.get("require_session_bootstrap", False):
        return {}
    if not session_id or not SESSION_ID_RE.fullmatch(session_id):
        raise GatewayError("BOOTSTRAP_REQUIRED: session_id ausente ou inválido", EXIT_SESSION_REQUIRED)
    reservation = secure_state_path(policy, "sessions", f"{session_id}.json")
    if not reservation.is_file():
        raise GatewayError("BOOTSTRAP_REQUIRED: reserva de sessão inexistente", EXIT_SESSION_REQUIRED)
    try:
        data = json.loads(read_state_text(policy, reservation))
    except (OSError, json.JSONDecodeError) as exc:
        raise GatewayError("BOOTSTRAP_REQUIRED: reserva de sessão inválida", EXIT_SESSION_REQUIRED) from exc
    if data.get("session_id") != session_id or data.get("status") not in {"reserved", "materialized"}:
        raise GatewayError("BOOTSTRAP_REQUIRED: reserva de sessão inativa", EXIT_SESSION_REQUIRED)
    validate_session_snapshot(policy, session_id, data)
    cwd_norm = norm(cwd)
    repo_root = data.get("repo_root", "")
    worktree = data.get("reserved_worktree", "")
    if not (pattern_match(cwd_norm, repo_root) or pattern_match(cwd_norm, worktree)):
        raise GatewayError("diretório não pertence à sessão reservada", EXIT_SESSION_REQUIRED)
    if risk == 2 and (data.get("status") != "materialized" or not pattern_match(cwd_norm, worktree)):
        raise GatewayError("risco 2 exige worktree materializado da sessão", EXIT_SESSION_REQUIRED)
    return data


@contextmanager
def repository_lock(state: GitState, policy: dict[str, Any], correlation_id: str) -> Iterator[Path]:
    secure_state_directory(policy, "locks")
    lock_path = secure_state_path(policy, "locks", f"{state_key(state)}.lock")
    payload = {
        "pid": os.getpid(), "host": socket.gethostname(),
        "correlation_id": correlation_id, "created_at": utc_now(),
        "repo_root": norm(state.repo_root),
    }
    try:
        lock_identity = write_state_text(
            policy,
            lock_path,
            json.dumps(payload, ensure_ascii=False),
            exclusive=True,
        )
    except FileExistsError as exc:
        raise GatewayError(f"repositório já possui lock: {lock_path}", EXIT_LOCKED) from exc
    try:
        yield lock_path
    finally:
        try:
            unlink_state_file(policy, lock_path, expected_identity=lock_identity)
        except FileNotFoundError:
            pass


def append_event(policy: dict[str, Any], event: dict[str, Any]) -> None:
    log_path = secure_state_path(policy, "events.jsonl")
    safe = json.loads(redact(json.dumps(event, ensure_ascii=False)))
    write_state_text(
        policy,
        log_path,
        json.dumps(safe, ensure_ascii=False, sort_keys=True) + "\n",
        append=True,
    )


def emit_json(payload: dict[str, Any], *, file=None) -> None:
    print(json.dumps(payload, ensure_ascii=True), file=file)


def event_args(args: Sequence[str]) -> list[str]:
    return [redact(item) for item in args]


def inspect(cwd: Path, policy: dict[str, Any], correlation_id: str, session_id: str | None = None) -> int:
    cwd = canonical_existing_directory(cwd)
    assert_allowed_path(cwd, policy)
    before = git_state(cwd, bool(policy.get("require_git_repo", True)), policy)
    event = {
        "timestamp": utc_now(), "correlation_id": correlation_id,
        "action": "inspect", "cwd": norm(cwd), "risk": 1,
        "session_id": session_id, "result": "ok", "before": asdict(before) if before else None,
    }
    append_event(policy, event)
    emit_json(event)
    return 0


def execute(cwd: Path, policy: dict[str, Any], args: Sequence[str], risk: int,
            timeout: int, expected_head: str | None, allow_dirty: bool,
            allow_head_change: bool, correlation_id: str, session_id: str | None = None) -> int:
    cwd = canonical_existing_directory(cwd)
    assert_allowed_path(cwd, policy)
    validate_command(args, risk, policy, cwd)
    wsl_running_status = wsl_is_command(args)
    assert_safe_gui_automation(args, cwd, policy)
    max_timeout = int(policy.get("max_timeout_seconds", 900))
    if timeout < 1 or timeout > max_timeout:
        raise GatewayError(f"timeout deve estar entre 1 e {max_timeout}s")

    before = git_state(cwd, bool(policy.get("require_git_repo", True)), policy)
    if before is None:
        raise GatewayError("execução sem repositório não suportada no perfil atual")
    if expected_head and before.head.casefold() != expected_head.casefold():
        raise GatewayError("HEAD atual diverge do HEAD esperado")
    if risk == 2 and policy.get("risk2_requires_clean_tree", True) and before.status_count and not allow_dirty:
        raise GatewayError("risco 2 exige árvore limpa; use allow-dirty somente com autorização explícita")

    started = time.monotonic()
    with repository_lock(before, policy, correlation_id):
        locked_state = git_state(cwd, True, policy)
        if locked_state != before:
            raise GatewayError("estado mudou antes da execução; possível concorrência", EXIT_STATE_CHANGED)
        spawn_cwd = canonical_existing_directory(cwd)
        if lexical_norm(spawn_cwd) != lexical_norm(cwd):
            raise GatewayError(
                "cwd mudou antes da execução; possível concorrência",
                EXIT_STATE_CHANGED,
            )
        with anchored_subprocess_cwd(spawn_cwd) as (anchored_cwd, spawn_options):
            try:
                if wsl_running_status:
                    # The trusted helper only executes --list --running --quiet,
                    # never starts Ubuntu or passes through untrusted output.
                    completed = wsl_run_fixed(
                        args, risk, policy, cwd=anchored_cwd,
                        timeout=timeout, spawn_options=spawn_options,
                    )
                else:
                    completed = subprocess.run(
                        list(args), cwd=anchored_cwd, text=True, capture_output=True,
                        encoding="utf-8", errors="replace", shell=False, timeout=timeout,
                        check=False, **spawn_options,
                    )
            except subprocess.TimeoutExpired as exc:
                event = {
                    "timestamp": utc_now(), "correlation_id": correlation_id,
                    "action": "run", "cwd": norm(cwd), "risk": risk,
                    "session_id": session_id, "command": event_args(args), "result": "timeout",
                    "timeout_seconds": timeout, "before": asdict(before),
                }
                append_event(policy, event)
                raise GatewayError("processo excedeu timeout", EXIT_TIMEOUT) from exc

        after = git_state(cwd, True, policy)
        state_changed = after != before
        duration_ms = int((time.monotonic() - started) * 1000)
        result = "ok" if completed.returncode == 0 else "command_failed"
        exit_code = completed.returncode if completed.returncode != 0 else 0
        if risk == 1 and state_changed:
            result, exit_code = "blocked_false_positive_state_change", EXIT_STATE_CHANGED
        elif risk == 2 and after.head != before.head and not allow_head_change:
            result, exit_code = "blocked_unexpected_head_change", EXIT_STATE_CHANGED

        event = {
            "timestamp": utc_now(), "correlation_id": correlation_id,
            "action": "run", "cwd": norm(cwd), "risk": risk,
            "session_id": session_id, "command": event_args(args), "command_exit_code": completed.returncode,
            "gateway_exit_code": exit_code, "duration_ms": duration_ms,
            "state_changed": state_changed, "result": result,
            "before": asdict(before), "after": asdict(after),
            "stdout": redact(completed.stdout[-12000:]),
            "stderr": redact(completed.stderr[-12000:]),
        }
        append_event(policy, event)
        emit_json(event)
        if exit_code == 0:
            return 0
        if exit_code == EXIT_STATE_CHANGED:
            return EXIT_STATE_CHANGED
        return completed.returncode if 1 <= completed.returncode <= 125 else EXIT_COMMAND


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ReqSys Command Gateway local")
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--correlation-id", default=None)
    sub = parser.add_subparsers(dest="action", required=True)
    inspect_parser = sub.add_parser("inspect")
    inspect_parser.add_argument("--cwd", type=Path, required=True)
    inspect_parser.add_argument("--session-id")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--cwd", type=Path, required=True)
    run_parser.add_argument("--session-id")
    run_parser.add_argument("--risk", type=int, required=True)
    run_parser.add_argument("--timeout", type=int, default=120)
    run_parser.add_argument("--expected-head")
    run_parser.add_argument("--allow-dirty", action="store_true")
    run_parser.add_argument("--allow-head-change", action="store_true")
    run_parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main() -> int:
    ns = build_parser().parse_args()
    correlation_id = ns.correlation_id or str(uuid.uuid4())
    policy: dict[str, Any] | None = None
    try:
        policy = load_policy(ns.policy)
        if ns.action in {"inspect", "run"}:
            ns.cwd = canonical_existing_directory(ns.cwd)
            enforce_session(
                policy,
                getattr(ns, "session_id", None),
                ns.cwd,
                getattr(ns, "risk", None),
            )
        if ns.action == "inspect":
            return inspect(ns.cwd, policy, correlation_id, getattr(ns, "session_id", None))
        command = list(ns.command)
        if command and command[0] == "--":
            command = command[1:]
        return execute(
            cwd=ns.cwd, policy=policy, args=command, risk=ns.risk,
            timeout=ns.timeout, expected_head=ns.expected_head,
            allow_dirty=ns.allow_dirty, allow_head_change=ns.allow_head_change,
            correlation_id=correlation_id, session_id=getattr(ns, "session_id", None),
        )
    except (GatewayError, json.JSONDecodeError, OSError) as exc:
        code = exc.exit_code if isinstance(exc, GatewayError) else EXIT_POLICY
        error = {
            "timestamp": utc_now(), "correlation_id": correlation_id,
            "result": "blocked", "gateway_exit_code": code,
            "error": redact(str(exc)),
        }
        if policy is not None:
            blocked_event = dict(error)
            blocked_event["action"] = getattr(ns, "action", "unknown")
            if hasattr(ns, "cwd"):
                blocked_event["cwd"] = norm(ns.cwd)
            if hasattr(ns, "risk"):
                blocked_event["risk"] = ns.risk
            if hasattr(ns, "session_id"):
                blocked_event["session_id"] = ns.session_id
            if hasattr(ns, "command"):
                command = list(ns.command)
                if command and command[0] == "--":
                    command = command[1:]
                blocked_event["command"] = event_args(command)
            try:
                append_event(policy, blocked_event)
            except OSError:
                pass
        emit_json(error, file=sys.stderr)
        return code


if __name__ == "__main__":
    raise SystemExit(main())

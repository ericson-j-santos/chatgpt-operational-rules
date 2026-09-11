#!/usr/bin/env python3
"""Command Gateway local: execução governada, auditável e sem shell intermediário."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import socket
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
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")


class GatewayError(RuntimeError):
    def __init__(self, message: str, exit_code: int = EXIT_POLICY) -> None:
        super().__init__(message)
        self.exit_code = exit_code


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
    value = SECRET_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", value)
    value = BEARER_RE.sub("Bearer [REDACTED]", value)
    return TOKEN_RE.sub("[REDACTED_TOKEN]", value)


def expand_path(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value))


def norm(value: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(expand_path(os.fspath(value)))).replace("\\", "/")


def pattern_match(path: str, pattern: str) -> bool:
    p = norm(path)
    q = norm(pattern)
    if any(ch in q for ch in "*?[]"):
        return fnmatch.fnmatchcase(p, q)
    return p == q or p.startswith(q.rstrip("/") + "/")


def load_policy(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1:
        raise GatewayError("versão de política não suportada")
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
        if SECRET_RE.search(raw) or BEARER_RE.search(raw) or TOKEN_RE.search(raw):
            return True
        parts = [part for part in token.split("/") if part]
        if any(part.casefold() in denied_segments for part in parts):
            return True
        basename = parts[-1] if parts else token
        if any(fnmatch.fnmatchcase(basename.casefold(), pat.casefold()) for pat in denied_names):
            return True
    return False


def validate_command(args: Sequence[str], risk: int, policy: dict[str, Any]) -> None:
    if risk not in (1, 2):
        raise GatewayError("risco 3 não é executado automaticamente")
    if not args:
        raise GatewayError("comando ausente")
    exe = Path(args[0]).name.casefold()
    if exe.endswith(".exe"):
        exe = exe[:-4]
    allowed = {item.casefold() for item in policy.get("allowed_executables", [])}
    blocked = {item.casefold() for item in policy.get("blocked_executables", [])}
    if exe in blocked or exe not in allowed:
        raise GatewayError(f"executável não autorizado: {exe}")
    if any(meta in arg for arg in args for meta in SHELL_META):
        raise GatewayError("composição/redirecionamento de shell bloqueado")
    if sensitive_reference(args, policy):
        raise GatewayError("referência potencialmente sensível bloqueada")
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


def run_capture(args: Sequence[str], cwd: Path, timeout: int = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args), cwd=str(cwd), text=True, capture_output=True,
        shell=False, timeout=timeout, check=False,
    )


def git_state(cwd: Path, require_repo: bool = True, policy: dict[str, Any] | None = None) -> GitState | None:
    root = run_capture(["git", "rev-parse", "--show-toplevel"], cwd)
    if root.returncode != 0:
        if require_repo:
            raise GatewayError("diretório não é repositório Git")
        return None
    if root.stderr.strip():
        raise GatewayError("estado Git incompleto: rev-parse produziu aviso/erro em stderr", EXIT_STATE_CHANGED)
    repo_root = Path(root.stdout.strip())
    head = run_capture(["git", "rev-parse", "HEAD"], repo_root)
    branch = run_capture(["git", "branch", "--show-current"], repo_root)
    tracked = run_capture(["git", "status", "--porcelain=v1", "-z", "--untracked-files=no"], repo_root)
    pathspec = ["."] + [f":(exclude){item}" for item in (policy or {}).get("git_untracked_excludes", [])]
    untracked = run_capture(["git", "ls-files", "--others", "--exclude-standard", "-z", "--", *pathspec], repo_root)
    checks = (head, branch, tracked, untracked)
    if any(item.returncode != 0 for item in checks):
        raise GatewayError("não foi possível obter estado Git")
    if any(item.stderr.strip() for item in checks):
        raise GatewayError("estado Git incompleto: comando Git produziu aviso/erro em stderr", EXIT_STATE_CHANGED)
    raw = ("T\0" + tracked.stdout + "U\0" + untracked.stdout).encode("utf-8", errors="replace")
    count = sum(len([x for x in item.stdout.split("\x00") if x]) for item in (tracked, untracked))
    return GitState(repo_root=str(repo_root), branch=branch.stdout.strip() or "DETACHED", head=head.stdout.strip(), status_digest=hashlib.sha256(raw).hexdigest(), status_count=count)


def state_key(state: GitState) -> str:
    return hashlib.sha256(norm(state.repo_root).encode("utf-8")).hexdigest()[:24]


def state_dir(policy: dict[str, Any]) -> Path:
    configured = expand_path(policy.get("state_dir", ""))
    if not configured or "%" in configured:
        configured = str(Path.home() / ".reqsys-command-gateway")
    path = Path(configured)
    path.mkdir(parents=True, exist_ok=True)
    return path


def snapshot_sha256(payload: dict[str, Any]) -> str:
    body = {key: value for key, value in payload.items() if key != "snapshot_sha256"}
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def validate_session_snapshot(policy: dict[str, Any], session_id: str, reservation: dict[str, Any]) -> dict[str, Any] | None:
    if not policy.get("require_preflight_snapshot", False):
        return None
    path = state_dir(policy) / "snapshots" / f"{session_id}.json"
    if not path.is_file():
        raise GatewayError("BOOTSTRAP_REQUIRED: snapshot de preflight inexistente", EXIT_SESSION_REQUIRED)
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
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
    reservation = state_dir(policy) / "sessions" / f"{session_id}.json"
    if not reservation.is_file():
        raise GatewayError("BOOTSTRAP_REQUIRED: reserva de sessão inexistente", EXIT_SESSION_REQUIRED)
    try:
        data = json.loads(reservation.read_text(encoding="utf-8"))
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
    lock_dir = state_dir(policy) / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{state_key(state)}.lock"
    payload = {
        "pid": os.getpid(), "host": socket.gethostname(),
        "correlation_id": correlation_id, "created_at": utc_now(),
        "repo_root": norm(state.repo_root),
    }
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise GatewayError(f"repositório já possui lock: {lock_path}", EXIT_LOCKED) from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        yield lock_path
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def append_event(policy: dict[str, Any], event: dict[str, Any]) -> None:
    log_path = state_dir(policy) / "events.jsonl"
    safe = json.loads(redact(json.dumps(event, ensure_ascii=False)))
    with log_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(safe, ensure_ascii=False, sort_keys=True) + "\n")


def event_args(args: Sequence[str]) -> list[str]:
    return [redact(item) for item in args]


def inspect(cwd: Path, policy: dict[str, Any], correlation_id: str, session_id: str | None = None) -> int:
    assert_allowed_path(cwd, policy)
    before = git_state(cwd, bool(policy.get("require_git_repo", True)), policy)
    event = {
        "timestamp": utc_now(), "correlation_id": correlation_id,
        "action": "inspect", "cwd": norm(cwd), "risk": 1,
        "session_id": session_id, "result": "ok", "before": asdict(before) if before else None,
    }
    append_event(policy, event)
    print(json.dumps(event, ensure_ascii=False))
    return 0


def execute(cwd: Path, policy: dict[str, Any], args: Sequence[str], risk: int,
            timeout: int, expected_head: str | None, allow_dirty: bool,
            allow_head_change: bool, correlation_id: str, session_id: str | None = None) -> int:
    assert_allowed_path(cwd, policy)
    validate_command(args, risk, policy)
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
        try:
            completed = subprocess.run(
                list(args), cwd=str(cwd), text=True, capture_output=True,
                shell=False, timeout=timeout, check=False,
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
        print(json.dumps(event, ensure_ascii=False))
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
            enforce_session(policy, getattr(ns, "session_id", None), ns.cwd, getattr(ns, "risk", None))
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
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        return code


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Inicializador único de sessão governada para o Command Gateway."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# The launcher must not create import caches before a session is authorized.
sys.dont_write_bytecode = True

# Windows embeddable Python can run with pythonXY._pth isolation, which omits
# the script directory from sys.path. Bootstrap sibling imports explicitly so
# the governed launcher behaves identically under system and portable Python.
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import command_gateway as cg
import session_bootstrap as sb
import session_preflight as sp
import runner_version_preflight as rvp

EXIT_SESSION_LAUNCHER = 28
MIN_RULES_VERSION = (1, 5, 2)
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
HEAD_RE = re.compile(r"^[0-9a-fA-F]{40}$")
SYNC_REF_RE = re.compile(
    r"^(?P<remote>[A-Za-z0-9][A-Za-z0-9._-]*)/(?P<branch>[A-Za-z0-9][A-Za-z0-9._/-]*)$"
)

PhaseCallback = Callable[[str], None]
DIAGNOSTIC_ENV_KEYS = (
    "LOCALAPPDATA",
    "USERPROFILE",
    "REQSYS_WORKSPACE_ROOT",
    "REQSYS_GATEWAY_STATE_DIR",
)
DIAGNOSTIC_IDENTIFIER_LIMIT = 256


def parse_version(value: str) -> tuple[int, int, int]:
    match = VERSION_RE.fullmatch(str(value).strip())
    if not match:
        raise cg.GatewayError("rules_version inválida para o session launcher", EXIT_SESSION_LAUNCHER)
    return tuple(int(part) for part in match.groups())


def ensure_min_version(policy: dict) -> str:
    raw = str(policy.get("rules_version", ""))
    if parse_version(raw) < MIN_RULES_VERSION:
        raise cg.GatewayError(
            "Session Launcher exige Command Gateway >= 1.5.2",
            EXIT_SESSION_LAUNCHER,
        )
    return raw


def enforce_runner_version_preflight() -> dict:
    try:
        installed = rvp.detect_runner_version()
        token = os.environ.get("RUNNER_DEPRECATION_API_TOKEN") or os.environ.get("GITHUB_TOKEN")
        evidence = rvp.build_evidence(installed=installed, token=token)
    except Exception as exc:
        detail = cg.redact(str(exc))[:160]
        raise cg.GatewayError(
            f"runner version preflight failed: {type(exc).__name__}:{detail}",
            EXIT_SESSION_LAUNCHER,
        ) from exc
    if evidence.get("ok") is not True:
        raise cg.GatewayError(
            "runner version preflight blocked: "
            f"installed={evidence.get('installed_version')} "
            f"registration_supported={evidence.get('registration_supported')} "
            f"runtime_supported_now={evidence.get('runtime_supported_now')}",
            EXIT_SESSION_LAUNCHER,
        )
    return evidence


def _set_phase(callback: PhaseCallback | None, phase: str) -> None:
    if callback is not None:
        callback(phase)


def _diagnostic_text(value: object) -> str:
    return cg.redact(str(value))


def _diagnostic_identifier(value: object, field: str) -> str:
    raw = str(value or "")
    if (
        not raw
        or len(raw) > DIAGNOSTIC_IDENTIFIER_LIMIT
        or any(ord(character) < 32 or ord(character) == 127 for character in raw)
    ):
        raise cg.GatewayError(
            f"{field} inválido para diagnóstico",
            EXIT_SESSION_LAUNCHER,
        )
    return _diagnostic_text(raw)


def _stable_file_bytes(path: Path, label: str) -> bytes:
    before = path.stat()
    content = path.read_bytes()
    after = path.stat()
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_identity != after_identity or len(content) != after.st_size:
        raise cg.GatewayError(
            f"{label} mudou durante o diagnóstico",
            EXIT_SESSION_LAUNCHER,
        )
    return content


def _load_diagnostic_policy(path: Path) -> tuple[dict, str]:
    raw = _stable_file_bytes(path, "policy")
    try:
        policy = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise cg.GatewayError(
            "policy inválida para diagnóstico",
            EXIT_SESSION_LAUNCHER,
        ) from exc
    if not isinstance(policy, dict):
        raise cg.GatewayError(
            "policy deve ser objeto para diagnóstico",
            EXIT_SESSION_LAUNCHER,
        )
    version = policy.get("version")
    if type(version) is not int or version != 1:
        raise cg.GatewayError(
            "versão de policy não suportada",
            EXIT_SESSION_LAUNCHER,
        )
    for key in ("session_source_roots", "allowed_roots", "denied_roots"):
        configured = policy.get(key)
        if not isinstance(configured, list):
            raise cg.GatewayError(
                f"{key} deve ser lista para diagnóstico",
                EXIT_SESSION_LAUNCHER,
            )
        for index, entry in enumerate(configured):
            if not isinstance(entry, str) or not entry.strip():
                raise cg.GatewayError(
                    f"{key}[{index}] deve ser string não vazia para diagnóstico",
                    EXIT_SESSION_LAUNCHER,
                )
    try:
        cg.validate_policy(policy, enforce_platform=False)
    except cg.GatewayError as exc:
        raise cg.GatewayError(
            f"policy inválida para diagnóstico: {exc}",
            EXIT_SESSION_LAUNCHER,
        ) from exc
    return policy, hashlib.sha256(raw).hexdigest()


def _python_started_without_bytecode() -> bool:
    return bool(sys.flags.dont_write_bytecode)


def _file_identity(name: str, path: Path) -> dict[str, object]:
    raw = _stable_file_bytes(path, f"módulo {name}")
    return {
        "name": name,
        "path": _diagnostic_text(cg.norm(path)),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "hash_scope": "stable_on_disk_snapshot_after_import",
        "loaded_bytes_attested": False,
    }


def diagnose(
    repo: Path,
    policy_path: Path,
    *,
    correlation_id: str = "diagnostic",
    phase_callback: PhaseCallback | None = None,
) -> dict:
    """Report launcher identity and path comparisons without executing bootstrap."""
    _set_phase(phase_callback, "diagnostic_load_policy")
    policy, policy_sha256 = _load_diagnostic_policy(policy_path)
    rules_version = ensure_min_version(policy)
    _set_phase(phase_callback, "diagnostic_hash_inputs")
    modules = [
        _file_identity("session_launcher", Path(__file__)),
        _file_identity("command_gateway", Path(cg.__file__)),
        _file_identity("session_bootstrap", Path(sb.__file__)),
        _file_identity("session_preflight", Path(sp.__file__)),
        _file_identity("runner_version_preflight", Path(rvp.__file__)),
    ]
    repo_norm = cg.norm(repo)

    _set_phase(phase_callback, "diagnostic_compare_paths")
    comparisons: dict[str, list[dict[str, object]]] = {}
    for key in ("session_source_roots", "allowed_roots", "denied_roots"):
        configured = policy.get(key, [])
        if not isinstance(configured, list):
            raise cg.GatewayError(
                f"{key} deve ser lista para diagnóstico",
                EXIT_SESSION_LAUNCHER,
            )
        items: list[dict[str, object]] = []
        for raw in configured:
            value = str(raw or "")
            try:
                expanded = cg.expand_path(value)
                normalized = cg.norm(value)
            except cg.GatewayError:
                items.append(
                    {
                        "configured": _diagnostic_text(value),
                        "expanded": None,
                        "normalized": None,
                        "resolved": False,
                        "exact_match": False,
                        "pattern_match": False,
                    }
                )
                continue
            items.append(
                {
                    "configured": _diagnostic_text(value),
                    "expanded": _diagnostic_text(expanded),
                    "normalized": _diagnostic_text(normalized),
                    "resolved": True,
                    "exact_match": repo_norm == normalized,
                    "pattern_match": cg.pattern_match(repo_norm, value),
                }
            )
        comparisons[key] = items

    environment = {
        key: None if os.environ.get(key) is None else _diagnostic_text(os.environ[key])
        for key in DIAGNOSTIC_ENV_KEYS
    }
    source_exact = any(
        item["exact_match"] for item in comparisons["session_source_roots"]
    )
    _set_phase(phase_callback, "diagnostic_complete")
    return {
        "timestamp": cg.utc_now(),
        "correlation_id": _diagnostic_identifier(correlation_id, "correlation_id"),
        "action": "session_launcher_diagnose",
        "result": "DIAGNOSTIC_OK",
        "phase": "diagnostic_complete",
        "execution_authorized": False,
        "state_validated": False,
        "launch_invoked": False,
        "policy": {
            "path": _diagnostic_text(policy_path),
            "normalized": _diagnostic_text(cg.norm(policy_path)),
            "sha256": policy_sha256,
            "rules_version": _diagnostic_text(rules_version),
            "hash_attests_parsed_bytes": True,
        },
        "modules": modules,
        "module_hash_limit": (
            "Hashes attest stable on-disk snapshots after import; "
            "already loaded module bytes are not attested."
        ),
        "cwd": {
            "value": _diagnostic_text(Path.cwd()),
            "normalized": _diagnostic_text(cg.norm(Path.cwd())),
        },
        "repo": {
            "value": _diagnostic_text(repo),
            "normalized": _diagnostic_text(repo_norm),
        },
        "environment": environment,
        "comparisons": comparisons,
        "session_source_exact_match": source_exact,
    }


def slug(value: str, limit: int) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-_").lower()
    return (cleaned or "session")[:limit]


def generate_session_id(repo: Path, prefix: str | None = None) -> str:
    base = slug(prefix or repo.name, 16)
    host = slug(socket.gethostname(), 12)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    suffix = f"{os.getpid():x}"
    return sb.validate_session_id(f"{base}-{host}-{stamp}-{suffix}"[:64])


def validate_expected_head(value: str | None) -> str | None:
    if value is None:
        return None
    if not HEAD_RE.fullmatch(value):
        raise cg.GatewayError("expected_head deve ser SHA completo de 40 caracteres", EXIT_SESSION_LAUNCHER)
    return value.lower()


def validate_sync_ref(value: str | None) -> tuple[str, str] | None:
    if value is None:
        return None
    raw = str(value).strip()
    if (
        not raw
        or raw.startswith("-")
        or raw.endswith("/")
        or "//" in raw
        or ".." in raw.split("/")
        or "\\" in raw
        or ":" in raw
        or "@{" in raw
    ):
        raise cg.GatewayError("sync_ref inválida", EXIT_SESSION_LAUNCHER)
    match = SYNC_REF_RE.fullmatch(raw)
    if not match:
        raise cg.GatewayError("sync_ref deve usar o formato remote/branch", EXIT_SESSION_LAUNCHER)
    return match.group("remote"), match.group("branch")


def _github_remote_matches_repository(url: str, repository: str) -> bool:
    normalized = str(url or "").strip().rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    expected = repository.casefold()
    candidates = {
        f"https://github.com/{repository}".casefold(),
        f"http://github.com/{repository}".casefold(),
        f"git@github.com:{repository}".casefold(),
        f"ssh://git@github.com/{repository}".casefold(),
    }
    return normalized.casefold() in candidates


def _github_actions_workspace_source(repo: Path, policy: dict) -> bool:
    if os.environ.get("GITHUB_ACTIONS", "").strip().casefold() != "true":
        return False
    workspace_raw = os.environ.get("GITHUB_WORKSPACE", "").strip()
    if not workspace_raw:
        return False
    workspace = Path(workspace_raw)
    if not workspace.is_absolute() or cg.norm(repo) != cg.norm(workspace):
        return False

    github_sha = os.environ.get("GITHUB_SHA", "").strip()
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not HEAD_RE.fullmatch(github_sha):
        raise cg.GatewayError("GITHUB_SHA inválido no checkout transitório", EXIT_SESSION_LAUNCHER)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise cg.GatewayError("GITHUB_REPOSITORY inválido no checkout transitório", EXIT_SESSION_LAUNCHER)

    parts = {part.casefold() for part in Path(cg.norm(repo)).parts}
    denied = {item.casefold() for item in policy.get("denied_segments", [])}
    if parts & denied:
        raise cg.GatewayError(
            "GITHUB_WORKSPACE contém segmento sensível bloqueado",
            EXIT_SESSION_LAUNCHER,
        )

    remote = cg.run_capture(["git", "remote", "get-url", "origin"], repo)
    if remote.returncode != 0 or remote.stderr.strip() or not remote.stdout.strip():
        raise cg.GatewayError(
            "GITHUB_WORKSPACE sem remoto origin validável",
            EXIT_SESSION_LAUNCHER,
        )
    if not _github_remote_matches_repository(remote.stdout.strip(), repository):
        raise cg.GatewayError(
            "GITHUB_WORKSPACE remoto diverge de GITHUB_REPOSITORY",
            EXIT_SESSION_LAUNCHER,
        )
    return True

def validate_session_source(repo: Path, policy: dict) -> bool:
    """Valida checkout transitório que só pode alimentar uma base isolada."""
    repo_norm = cg.norm(repo)
    if _github_actions_workspace_source(repo, policy):
        return True
    roots = policy.get("session_source_roots", [])
    if not isinstance(roots, list):
        raise cg.GatewayError("session_source_roots deve ser lista", EXIT_SESSION_LAUNCHER)
    for raw in roots:
        value = str(raw or "").strip()
        if not value:
            continue
        if any(ch in value for ch in "*?[]"):
            raise cg.GatewayError(
                "session_source_roots deve usar caminhos exatos, sem curingas",
                EXIT_SESSION_LAUNCHER,
            )
        if repo_norm != cg.norm(value):
            continue
        parts = {part.casefold() for part in Path(repo_norm).parts}
        denied = {item.casefold() for item in policy.get("denied_segments", [])}
        if parts & denied:
            raise cg.GatewayError(
                "fonte transitória contém segmento sensível bloqueado",
                EXIT_SESSION_LAUNCHER,
            )
        return True
    cg.assert_allowed_path(repo, policy)
    return False


def tracked_tree_dirty(repo: Path, policy: dict) -> bool:
    result = cg.run_capture(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=no"],
        repo,
        timeout=int(policy.get("git_state_timeout_seconds", 15)),
    )
    if result.returncode != 0 or result.stderr.strip():
        raise cg.GatewayError(
            "não foi possível validar alterações rastreadas antes da sincronização",
            cg.EXIT_STATE_CHANGED,
        )
    return bool(result.stdout)


def _fetch_and_verify_remote(
    repo: Path,
    expected: str,
    sync_ref: str,
) -> tuple[str, str, str]:
    parsed = validate_sync_ref(sync_ref)
    assert parsed is not None
    remote, branch = parsed

    fetched = cg.run_capture(
        ["git", "fetch", "--prune", "--no-tags", remote],
        repo,
        timeout=60,
    )
    if fetched.returncode != 0:
        detail = cg.redact((fetched.stderr or fetched.stdout).strip())[-300:]
        raise cg.GatewayError(
            f"sincronização recusada: fetch falhou: {detail}",
            cg.EXIT_STATE_CHANGED,
        )

    remote_ref = f"refs/remotes/{remote}/{branch}^{{commit}}"
    advertised = cg.run_capture(["git", "rev-parse", "--verify", remote_ref], repo)
    if advertised.returncode != 0:
        # A restrictive remote.fetch refspec can make an otherwise successful
        # fetch populate only FETCH_HEAD. Fetch exactly the pinned branch once,
        # never a wildcard or arbitrary ref provided by an untrusted caller.
        exact_refspec = f"refs/heads/{branch}:refs/remotes/{remote}/{branch}"
        pinned_fetch = cg.run_capture(
            ["git", "fetch", "--no-tags", remote, exact_refspec],
            repo,
            timeout=60,
        )
        if pinned_fetch.returncode != 0:
            raise cg.GatewayError(
                "sincronização recusada: referência remota não resolvida",
                cg.EXIT_STATE_CHANGED,
            )
        advertised = cg.run_capture(
            ["git", "rev-parse", "--verify", remote_ref], repo
        )
    if advertised.returncode != 0 or advertised.stderr.strip():
        raise cg.GatewayError(
            "sincronização recusada: referência remota não resolvida",
            cg.EXIT_STATE_CHANGED,
        )
    remote_head = advertised.stdout.strip().lower()
    if remote_head != expected:
        raise cg.GatewayError(
            f"sincronização recusada: {sync_ref}={remote_head} esperado={expected}",
            cg.EXIT_STATE_CHANGED,
        )

    source_url = cg.run_capture(["git", "remote", "get-url", remote], repo)
    if source_url.returncode != 0 or source_url.stderr.strip() or not source_url.stdout.strip():
        raise cg.GatewayError(
            "sincronização recusada: URL remota não pôde ser validada",
            cg.EXIT_STATE_CHANGED,
        )
    return remote, branch, source_url.stdout.strip()


def _isolated_base_path(policy: dict, session_id: str) -> Path:
    root = Path(cg.expand_path(policy.get("worktree_root", "C:\\dev")))
    target = root / f"session-base-{sb.validate_session_id(session_id)}"
    cg.assert_allowed_path(target, policy)
    return target


def _prepare_isolated_base(
    repo: Path,
    policy: dict,
    expected: str,
    remote: str,
    branch: str,
    source_url: str,
    session_id: str,
) -> Path:
    target = _isolated_base_path(policy, session_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        state = cg.git_state(target, True, policy)
        if state is None or state.head.lower() != expected or state.status_count:
            raise cg.GatewayError(
                "base isolada existente diverge do estado esperado",
                cg.EXIT_STATE_CHANGED,
            )
        configured = cg.run_capture(["git", "remote", "get-url", remote], target)
        if (
            configured.returncode != 0
            or configured.stderr.strip()
            or configured.stdout.strip() != source_url
        ):
            raise cg.GatewayError(
                "base isolada existente possui remoto divergente",
                cg.EXIT_STATE_CHANGED,
            )
        return target

    cloned = cg.run_capture(
        ["git", "clone", "--no-checkout", "--local", str(repo), str(target)],
        repo,
        timeout=120,
    )
    if cloned.returncode != 0:
        detail = cg.redact((cloned.stderr or cloned.stdout).strip())[-300:]
        raise cg.GatewayError(
            f"sincronização recusada: clone isolado falhou: {detail}",
            cg.EXIT_STATE_CHANGED,
        )

    set_remote = cg.run_capture(["git", "remote", "set-url", "origin", source_url], target)
    if set_remote.returncode != 0 or set_remote.stderr.strip():
        raise cg.GatewayError(
            "sincronização recusada: remoto da base isolada não pôde ser configurado",
            cg.EXIT_STATE_CHANGED,
        )

    if remote != "origin":
        add_remote = cg.run_capture(["git", "remote", "add", remote, source_url], target)
        if add_remote.returncode != 0 or add_remote.stderr.strip():
            raise cg.GatewayError(
                "sincronização recusada: remoto solicitado não pôde ser configurado na base isolada",
                cg.EXIT_STATE_CHANGED,
            )

    # _fetch_and_verify_remote already fetched and SHA-checked the source
    # branch before this isolated clone. --local copies those Git objects.
    # A second network fetch here is redundant and can fail after the first
    # attestation (for example, on Windows with a transient socket failure).
    # Prove that the pinned *commit object* exists in the isolated object
    # database before checkout; never silently accept a missing SHA.
    pinned = cg.run_capture(
        ["git", "rev-parse", "--verify", f"{expected}^{{commit}}"], target
    )
    if (
        pinned.returncode != 0
        or pinned.stderr.strip()
        or pinned.stdout.strip().lower() != expected
    ):
        raise cg.GatewayError(
            "sincronização recusada: base isolada sem commit SHA validado",
            cg.EXIT_STATE_CHANGED,
        )

    checked_out = cg.run_capture(["git", "checkout", "--detach", expected], target, timeout=120)
    if checked_out.returncode != 0:
        detail = cg.redact((checked_out.stderr or checked_out.stdout).strip())[-300:]
        raise cg.GatewayError(
            f"sincronização recusada: checkout isolado falhou: {detail}",
            cg.EXIT_STATE_CHANGED,
        )

    state = cg.git_state(target, True, policy)
    if state is None or state.head.lower() != expected or state.status_count:
        raise cg.GatewayError(
            "base isolada não terminou limpa no SHA esperado",
            cg.EXIT_STATE_CHANGED,
        )
    collisions = cg.tracked_case_collisions(target)
    if collisions:
        details = "; ".join(f"{left} <-> {right}" for left, right in collisions[:5])
        raise cg.GatewayError(
            f"base isolada possui caminhos rastreados que colidem por casing: {details}",
            cg.EXIT_STATE_CHANGED,
        )
    return target


def sync_expected_head(
    repo: Path,
    policy: dict,
    expected: str,
    sync_ref: str,
    session_id: str,
    *,
    source_only: bool = False,
) -> tuple[cg.GitState, Path, str]:
    current = cg.git_state(repo, True, policy)
    assert current is not None
    remote, branch, source_url = _fetch_and_verify_remote(repo, expected, sync_ref)

    if source_only:
        if current.head.lower() != expected:
            raise cg.GatewayError(
                "fonte transitória deve estar exatamente no expected_head",
                cg.EXIT_STATE_CHANGED,
            )
        if tracked_tree_dirty(repo, policy):
            raise cg.GatewayError(
                "fonte transitória possui alterações rastreadas",
                cg.EXIT_STATE_CHANGED,
            )
        isolated = _prepare_isolated_base(
            repo=repo,
            policy=policy,
            expected=expected,
            remote=remote,
            branch=branch,
            source_url=source_url,
            session_id=session_id,
        )
        isolated_state = cg.git_state(isolated, True, policy)
        assert isolated_state is not None
        return isolated_state, isolated, "isolated_session_source"

    ancestor = cg.run_capture(
        ["git", "merge-base", "--is-ancestor", current.head, expected],
        repo,
    )
    if ancestor.returncode == 1:
        isolated = _prepare_isolated_base(
            repo=repo,
            policy=policy,
            expected=expected,
            remote=remote,
            branch=branch,
            source_url=source_url,
            session_id=session_id,
        )
        isolated_state = cg.git_state(isolated, True, policy)
        assert isolated_state is not None
        return isolated_state, isolated, "isolated_diverged_base"
    if ancestor.returncode != 0 or ancestor.stderr.strip():
        raise cg.GatewayError(
            "sincronização recusada: não foi possível provar ancestralidade",
            cg.EXIT_STATE_CHANGED,
        )

    if tracked_tree_dirty(repo, policy):
        isolated = _prepare_isolated_base(
            repo=repo,
            policy=policy,
            expected=expected,
            remote=remote,
            branch=branch,
            source_url=source_url,
            session_id=session_id,
        )
        isolated_state = cg.git_state(isolated, True, policy)
        assert isolated_state is not None
        return isolated_state, isolated, "isolated_dirty_base"

    if current.head.lower() == expected:
        return current, repo, "not_needed"

    advanced = cg.run_capture(["git", "merge", "--ff-only", expected], repo, timeout=60)
    if advanced.returncode != 0:
        detail = cg.redact((advanced.stderr or advanced.stdout).strip())[-300:]
        raise cg.GatewayError(
            f"sincronização recusada: fast-forward falhou: {detail}",
            cg.EXIT_STATE_CHANGED,
        )

    final = cg.git_state(repo, True, policy)
    assert final is not None
    if tracked_tree_dirty(repo, policy):
        raise cg.GatewayError(
            "sincronização incompleta: base deixou de estar limpa após fast-forward",
            cg.EXIT_STATE_CHANGED,
        )
    if final.head.lower() != expected:
        raise cg.GatewayError(
            f"sincronização incompleta: atual={final.head} esperado={expected}",
            cg.EXIT_STATE_CHANGED,
        )
    return final, repo, "fast_forward"


def launch(
    repo: Path,
    policy_path: Path,
    session_id: str | None,
    session_prefix: str | None,
    correlation_id: str,
    expected_head: str | None,
    sync_ref: str | None = None,
    require_runner_version_preflight: bool = False,
    phase_callback: PhaseCallback | None = None,
) -> dict:
    _set_phase(phase_callback, "load_policy")
    policy = cg.load_policy(policy_path)
    _set_phase(phase_callback, "validate_rules_version")
    rules_version = ensure_min_version(policy)
    _set_phase(phase_callback, "runner_version_preflight")
    runner_version_preflight = (
        enforce_runner_version_preflight()
        if require_runner_version_preflight
        else None
    )
    _set_phase(phase_callback, "validate_session_source")
    source_only = validate_session_source(repo, policy)
    _set_phase(phase_callback, "read_git_state")
    state = cg.git_state(repo, True, policy)
    assert state is not None
    _set_phase(phase_callback, "validate_arguments")
    expected = validate_expected_head(expected_head)
    validated_sync_ref = validate_sync_ref(sync_ref)
    if validated_sync_ref and expected is None:
        raise cg.GatewayError(
            "sync_ref exige expected_head explícito",
            EXIT_SESSION_LAUNCHER,
        )
    if source_only and (expected is None or validated_sync_ref is None):
        raise cg.GatewayError(
            "fonte transitória exige expected_head e sync_ref explícitos",
            EXIT_SESSION_LAUNCHER,
        )

    _set_phase(phase_callback, "resolve_session")
    resolved_session = (
        sb.validate_session_id(session_id)
        if session_id
        else generate_session_id(repo, session_prefix)
    )

    session_repo = repo
    base_sync = "not_needed"
    if expected and sync_ref is not None:
        _set_phase(phase_callback, "synchronize_base")
        state, session_repo, base_sync = sync_expected_head(
            repo=repo,
            policy=policy,
            expected=expected,
            sync_ref=sync_ref,
            session_id=resolved_session,
            source_only=source_only,
        )
    elif expected and state.head.lower() != expected:
        raise cg.GatewayError(
            f"HEAD atual diverge do esperado: atual={state.head} esperado={expected}",
            cg.EXIT_STATE_CHANGED,
        )

    _set_phase(phase_callback, "session_preflight")
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink):
        preflight = sp.preflight(
            repo=session_repo,
            policy_path=policy_path,
            session_id=resolved_session,
            correlation_id=correlation_id,
            materialize=True,
        )
    target_state = preflight["target_state"]
    result = {
        "timestamp": cg.utc_now(),
        "correlation_id": correlation_id,
        "action": "session_launcher",
        "result": "SESSION_LAUNCH_OK",
        "session_id": resolved_session,
        "source_repo_root": cg.norm(repo),
        "repo_root": preflight["repo_root"],
        "target_path": preflight["target_path"],
        "head": target_state["head"],
        "snapshot_sha256": preflight["snapshot_sha256"],
        "rules_version": rules_version,
        "host": preflight["host"],
        "state_validated": True,
        "base_sync": base_sync,
        "sync_ref": sync_ref,
        "runner_version_preflight": runner_version_preflight,
    }
    _set_phase(phase_callback, "append_event")
    cg.append_event(policy, result)
    _set_phase(phase_callback, "completed")
    cg.emit_json(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Inicializador único de sessão governada")
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--session-id")
    parser.add_argument("--session-prefix")
    parser.add_argument("--correlation-id")
    parser.add_argument("--expected-head")
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Emite diagnóstico read-only e encerra antes do launch/preflight.",
    )
    parser.add_argument(
        "--require-runner-version-preflight",
        action="store_true",
        help="Valida a versão do self-hosted runner dentro do bootstrap governado.",
    )
    parser.add_argument(
        "--sync-ref",
        help="Permite sincronização governada com expected-head usando remote/branch.",
    )
    ns = parser.parse_args()
    correlation_id = ns.correlation_id or f"launcher-{os.getpid()}"
    phase = {"value": "arguments_parsed"}

    def set_phase(value: str) -> None:
        phase["value"] = value

    try:
        if ns.diagnose:
            set_phase("diagnostic_validate_runtime")
            if not _python_started_without_bytecode():
                raise cg.GatewayError(
                    "diagnose exige inicialização do Python com -B para impedir "
                    "cache antes da execução do script",
                    EXIT_SESSION_LAUNCHER,
                )
            set_phase("diagnostic_validate_arguments")
            incompatible = [
                option
                for option, present in (
                    ("--session-id", ns.session_id is not None),
                    ("--session-prefix", ns.session_prefix is not None),
                    ("--expected-head", ns.expected_head is not None),
                    ("--sync-ref", ns.sync_ref is not None),
                    (
                        "--require-runner-version-preflight",
                        ns.require_runner_version_preflight,
                    ),
                )
                if present
            ]
            if incompatible:
                raise cg.GatewayError(
                    "diagnose não aceita argumentos de execução: " + ", ".join(incompatible),
                    EXIT_SESSION_LAUNCHER,
                )
            payload = diagnose(
                repo=ns.repo,
                policy_path=ns.policy,
                correlation_id=correlation_id,
                phase_callback=set_phase,
            )
            cg.emit_json(payload)
            return 0
        launch(
            repo=ns.repo,
            policy_path=ns.policy,
            session_id=ns.session_id,
            session_prefix=ns.session_prefix,
            correlation_id=correlation_id,
            expected_head=ns.expected_head,
            sync_ref=ns.sync_ref,
            require_runner_version_preflight=ns.require_runner_version_preflight,
            phase_callback=set_phase,
        )
        return 0
    except (cg.GatewayError, json.JSONDecodeError, OSError) as exc:
        code = exc.exit_code if isinstance(exc, cg.GatewayError) else EXIT_SESSION_LAUNCHER
        cg.emit_json(
            {
                "timestamp": cg.utc_now(),
                "correlation_id": _diagnostic_text(correlation_id),
                "action": "session_launcher",
                "result": "SESSION_LAUNCH_BLOCKED",
                "gateway_exit_code": code,
                "error": cg.redact(str(exc)),
                "phase": phase["value"],
            },
            file=sys.stderr,
        )
        return code


if __name__ == "__main__":
    raise SystemExit(main())

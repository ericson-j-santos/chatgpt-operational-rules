#!/usr/bin/env python3
"""Proposal-bound, fail-closed reconciliation for TODO Gateway PR #185."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import socket
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping


REPOSITORY = "ericson-j-santos/chatgpt-operational-rules"
BRANCH = "codex/todo-gateway-ci-matrix"
OLD_HEAD = "68b9098e4348495045c24e92bba8e6b912f6b3a9"
BASE_SHA = "d6af41001bf641727a35a990e7c93b4c757538cb"
EXPECTED_HOST = "desktop-rp23ogs"
CONFIRM = "RECONCILE-TODO-GATEWAY-PR185"
MANIFEST_PATH = "MANIFEST.json"
MESSAGE = "reconcile: replay TODO Gateway CI changes on reviewed main"
IDENTITY_NAME = "ReqSys Governed Reconciliation"
IDENTITY_EMAIL = "reqsys-reconciliation@example.invalid"
MAX_PROPOSAL_BYTES = 64 * 1024
OVERLAY_BLOBS = {
    ".github/workflows/validate-rules.yml": "80656d2af3214c3cf7fb0a7b4d0307dcb67c19cc",
    "scripts/generate_manifest.py": "f84abdb4990be77dc615c4f0297f94f09ec58063",
    "tests/test_validate_rules_policy_profiles.py": "df41136c9036a7028cd14d04686e8d01515cc78b",
}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")
CORRELATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")
PROPOSAL_KEYS = {
    "version", "repository", "branch", "old_head", "base_sha", "main_sha",
    "main_tree", "generated_at", "commit_date", "overlay_blobs",
    "expected_file_count", "expected_manifest_blob", "expected_tree",
    "expected_commit", "git_path", "git_sha256",
}
DANGEROUS_GIT_ENV = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_ASKPASS", "GIT_CEILING_DIRECTORIES",
    "GIT_COMMON_DIR", "GIT_CONFIG", "GIT_CONFIG_COUNT", "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_SYSTEM",
    "GIT_DIR", "GIT_DISCOVERY_ACROSS_FILESYSTEM", "GIT_EXEC_PATH",
    "GIT_EXTERNAL_DIFF", "GIT_INDEX_FILE", "GIT_NAMESPACE",
    "GIT_OBJECT_DIRECTORY", "GIT_PROXY_COMMAND", "GIT_REPLACE_REF_BASE",
    "GIT_SHALLOW_FILE", "GIT_SSH", "GIT_SSH_COMMAND", "GIT_SSH_VARIANT",
    "GIT_TEMPLATE_DIR", "GIT_WORK_TREE",
}
_ACTIVE_GIT: Path | None = None


class ReconcileBlocked(RuntimeError):
    pass


def _assert_clean_git_environment() -> None:
    for key in os.environ:
        normalized = key.upper()
        if (
            normalized in DANGEROUS_GIT_ENV
            or normalized.startswith("GIT_CONFIG_KEY_")
            or normalized.startswith("GIT_CONFIG_VALUE_")
            or normalized.startswith("GIT_TRACE")
        ):
            raise ReconcileBlocked("git_environment_unsafe")


def _plain_path(path: Path, *, directory: bool) -> None:
    try:
        absolute = path.absolute()
        resolved = path.resolve(strict=True)
        metadata = os.lstat(path)
    except OSError as exc:
        raise ReconcileBlocked("path_identity_unavailable") from exc
    reparse = bool(getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    if (
        str(absolute).casefold() != str(resolved).casefold()
        or stat.S_ISLNK(metadata.st_mode)
        or reparse
        or (directory and not stat.S_ISDIR(metadata.st_mode))
        or (not directory and not stat.S_ISREG(metadata.st_mode))
    ):
        raise ReconcileBlocked("path_identity_redirected")


def _activate_git(proposal: Mapping[str, Any]) -> None:
    global _ACTIVE_GIT
    raw_path = proposal.get("git_path")
    digest = proposal.get("git_sha256")
    if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
        raise ReconcileBlocked("git_identity_invalid")
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise ReconcileBlocked("git_identity_invalid")
    binary = Path(raw_path)
    _plain_path(binary, directory=False)
    try:
        observed = hashlib.sha256(binary.read_bytes()).hexdigest()
    except OSError as exc:
        raise ReconcileBlocked("git_identity_unavailable") from exc
    if observed != digest:
        raise ReconcileBlocked("git_identity_mismatch")
    _ACTIVE_GIT = binary


def _run_git(
    repo: Path,
    *args: str,
    env: Mapping[str, str] | None = None,
    input_bytes: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    if _ACTIVE_GIT is None:
        raise ReconcileBlocked("git_identity_not_activated")
    child_env = os.environ.copy()
    if env:
        child_env.update(env)
    result = subprocess.run(
        [
            str(_ACTIVE_GIT),
            "-c", "core.fsmonitor=false",
            "-c", "http.followRedirects=false",
            "-C", str(repo),
            *args,
        ],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=child_env,
        timeout=60,
        check=False,
    )
    if check and result.returncode:
        raise ReconcileBlocked("git_operation_failed")
    return result


def _text(repo: Path, *args: str, env: Mapping[str, str] | None = None) -> str:
    return _run_git(repo, *args, env=env).stdout.decode("utf-8", "replace").strip()


def _sha(repo: Path, rev: str) -> str:
    value = _text(repo, "rev-parse", rev).lower()
    if not SHA_RE.fullmatch(value):
        raise ReconcileBlocked("object_identity_invalid")
    return value


def _tree(repo: Path, commit: str) -> str:
    return _sha(repo, f"{commit}^{{tree}}")


def _parents(repo: Path, commit: str) -> list[str]:
    return _text(repo, "show", "-s", "--format=%P", commit).lower().split()


def _remote_sha(repo: Path, branch: str) -> str:
    words = _text(repo, "ls-remote", "--heads", "origin", f"refs/heads/{branch}").split()
    value = words[0].lower() if words else ""
    if value and not SHA_RE.fullmatch(value):
        raise ReconcileBlocked("remote_identity_invalid")
    return value


def _canonical_remote(value: str) -> str:
    normalized = value.strip()
    if normalized.endswith("/"):
        normalized = normalized[:-1]
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    if normalized.startswith("https://github.com/"):
        repository = normalized.removeprefix("https://github.com/")
    elif normalized.startswith("git@github.com:"):
        repository = normalized.removeprefix("git@github.com:")
    else:
        raise ReconcileBlocked("remote_url_not_approved")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ReconcileBlocked("remote_url_not_approved")
    return repository.lower()


def _git_lines(repo: Path, *args: str) -> list[str]:
    result = _run_git(repo, *args)
    try:
        return [line for line in result.stdout.decode("utf-8").splitlines() if line]
    except UnicodeDecodeError as exc:
        raise ReconcileBlocked("git_output_invalid") from exc


def _assert_safe_git_config(repo: Path) -> None:
    unsafe = _run_git(
        repo,
        "config", "--name-only", "--get-regexp",
        r"^(core\.(fsmonitor|hookspath|sshcommand)|diff\.external|"
        r"remote\.origin\.(uploadpack|receivepack|proxy)|"
        r"filter\..*\.(clean|smudge|process|required)|"
        r"url\..*\.(insteadof|pushinsteadof))$",
        check=False,
    )
    if unsafe.returncode not in {0, 1}:
        raise ReconcileBlocked("git_config_inspection_failed")
    names = {line.strip().casefold() for line in unsafe.stdout.decode("utf-8", "replace").splitlines() if line.strip()}
    for name in names:
        value_result = _run_git(repo, "config", "--get", name, check=False)
        if value_result.returncode not in {0, 1}:
            raise ReconcileBlocked("git_config_inspection_failed")
        value = value_result.stdout.decode("utf-8", "replace").strip().casefold()
        if name == "core.fsmonitor" and value == "false":
            continue
        if name.startswith("filter."):
            if name.endswith(".required") and value in {"", "false", "no", "off", "0"}:
                continue
            if name.endswith((".clean", ".smudge", ".process")) and value == "":
                continue
        raise ReconcileBlocked("git_config_unsafe")


def _validate_remote_configuration(repo: Path) -> None:
    fetch_urls = _git_lines(repo, "remote", "get-url", "--all", "origin")
    push_urls = _git_lines(repo, "remote", "get-url", "--push", "--all", "origin")
    if len(fetch_urls) != 1 or len(push_urls) != 1:
        raise ReconcileBlocked("remote_url_count_invalid")
    if _canonical_remote(fetch_urls[0]) != REPOSITORY:
        raise ReconcileBlocked("origin_mismatch")
    if _canonical_remote(push_urls[0]) != REPOSITORY:
        raise ReconcileBlocked("push_origin_mismatch")


def _changed_paths(repo: Path, older: str, newer: str) -> set[str]:
    raw = _run_git(repo, "diff", "--name-only", "-z", older, newer).stdout
    return {item.decode("utf-8") for item in raw.split(b"\0") if item}


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReconcileBlocked("proposal_duplicate_key")
        result[key] = value
    return result


def _validate_timestamp(value: Any) -> None:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise ReconcileBlocked("proposal_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ReconcileBlocked("proposal_timestamp_invalid") from exc
    if parsed.utcoffset() != timedelta(0) or parsed.isoformat() != value:
        raise ReconcileBlocked("proposal_timestamp_invalid")


def _load_proposal(path: Path, expected_sha256: str) -> dict[str, Any]:
    if not SHA256_RE.fullmatch(expected_sha256):
        raise ReconcileBlocked("proposal_digest_invalid")
    try:
        absolute = path.absolute()
        resolved = path.resolve(strict=True)
        payload = resolved.read_bytes()
    except OSError as exc:
        raise ReconcileBlocked("proposal_unavailable") from exc
    if str(absolute).casefold() != str(resolved).casefold():
        raise ReconcileBlocked("proposal_path_redirected")
    if len(payload) > MAX_PROPOSAL_BYTES:
        raise ReconcileBlocked("proposal_too_large")
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ReconcileBlocked("proposal_digest_mismatch")
    try:
        proposal = json.loads(payload, object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReconcileBlocked("proposal_json_invalid") from exc
    if not isinstance(proposal, dict) or set(proposal) != PROPOSAL_KEYS:
        raise ReconcileBlocked("proposal_schema_invalid")
    if type(proposal.get("version")) is not int or proposal["version"] != 1:
        raise ReconcileBlocked("proposal_version_invalid")
    fixed = {
        "repository": REPOSITORY,
        "branch": BRANCH,
        "old_head": OLD_HEAD,
        "base_sha": BASE_SHA,
        "overlay_blobs": OVERLAY_BLOBS,
    }
    if any(proposal.get(key) != value for key, value in fixed.items()):
        raise ReconcileBlocked("proposal_scope_mismatch")
    for key in ("main_sha", "main_tree", "expected_manifest_blob", "expected_tree", "expected_commit"):
        if not isinstance(proposal.get(key), str) or not SHA_RE.fullmatch(proposal[key]):
            raise ReconcileBlocked("proposal_object_invalid")
    if (
        isinstance(proposal.get("expected_file_count"), bool)
        or not isinstance(proposal.get("expected_file_count"), int)
        or proposal["expected_file_count"] < 1
    ):
        raise ReconcileBlocked("proposal_file_count_invalid")
    for key in ("generated_at", "commit_date"):
        _validate_timestamp(proposal.get(key))
    return proposal


def _index_entries(repo: Path, index_env: Mapping[str, str]) -> dict[str, tuple[str, str]]:
    raw = _run_git(repo, "ls-files", "--stage", "-z", env=index_env).stdout
    entries: dict[str, tuple[str, str]] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, path_bytes = record.split(b"\t", 1)
            mode, blob, stage = metadata.decode("ascii").split()
            path = path_bytes.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ReconcileBlocked("index_entry_invalid") from exc
        if stage != "0" or mode not in {"100644", "100755"} or not SHA_RE.fullmatch(blob):
            raise ReconcileBlocked("index_entry_invalid")
        entries[path] = (mode, blob)
    return entries


def _canonical_manifest_payload(
    repo: Path,
    index_env: Mapping[str, str],
    entries: Mapping[str, tuple[str, str]],
    generated_at: str,
) -> bytes:
    with tempfile.TemporaryDirectory(prefix="pr185-canonical-manifest-") as temporary:
        # Match the generator's physical ROOT spelling (including Windows 8.3
        # aliases) while preserving the exact command allowlist below.
        root = Path(temporary).resolve(strict=True)
        _run_git(repo, "checkout-index", "--all", f"--prefix={root}{os.sep}", env=index_env)
        generator_path = root / "scripts" / "generate_manifest.py"
        module_name = "_reqsys_pr185_canonical_manifest"
        original_path = list(sys.path)
        original_dont_write = sys.dont_write_bytecode
        saved_modules = {
            name: sys.modules.pop(name, None)
            for name in (module_name, "validate_rules", "command_gateway")
        }
        try:
            spec = importlib.util.spec_from_file_location(module_name, generator_path)
            if spec is None or spec.loader is None:
                raise ReconcileBlocked("canonical_generator_unavailable")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            required = getattr(module, "REQUIRED_PATHS", None)
            if not isinstance(required, set) or not all(isinstance(item, str) for item in required):
                raise ReconcileBlocked("canonical_required_paths_invalid")
            if not required.issubset(entries):
                raise ReconcileBlocked("canonical_required_path_missing")
            fixed_time = datetime.fromisoformat(generated_at)

            class FixedDateTime:
                @classmethod
                def now(cls, _zone: object = None) -> datetime:
                    return fixed_time

            listed = b"\0".join(path.encode("utf-8") for path in sorted(entries)) + b"\0"

            class GitLsFilesOnly:
                @staticmethod
                def run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
                    expected = ["git", "-C", str(root), "ls-files", "-z"]
                    if list(args) != expected:
                        raise ReconcileBlocked("canonical_generator_external_command")
                    return subprocess.CompletedProcess(args, 0, listed, b"")

            module.datetime = FixedDateTime
            module.subprocess = GitLsFilesOnly
            if module.main() != 0:
                raise ReconcileBlocked("canonical_generator_failed")
            return (root / MANIFEST_PATH).read_bytes()
        except ReconcileBlocked:
            raise
        except (OSError, RuntimeError, SystemExit, ValueError) as exc:
            raise ReconcileBlocked("canonical_generator_failed") from exc
        finally:
            for name in (module_name, "validate_rules", "command_gateway"):
                sys.modules.pop(name, None)
                if saved_modules[name] is not None:
                    sys.modules[name] = saved_modules[name]
            sys.path[:] = original_path
            sys.dont_write_bytecode = original_dont_write


def _candidate(repo: Path, proposal: Mapping[str, Any]) -> dict[str, Any]:
    common_raw = Path(_text(repo, "rev-parse", "--git-common-dir"))
    common = common_raw if common_raw.is_absolute() else (repo / common_raw).resolve()
    descriptor, index_name = tempfile.mkstemp(prefix="pr185-index-", dir=common)
    os.close(descriptor)
    index_path = Path(index_name)
    index_path.unlink()
    index_env = {"GIT_INDEX_FILE": str(index_path)}
    try:
        _run_git(repo, "read-tree", proposal["main_sha"], env=index_env)
        for path, blob in OVERLAY_BLOBS.items():
            _run_git(repo, "update-index", "--add", "--cacheinfo", "100644", blob, path, env=index_env)
        entries = _index_entries(repo, index_env)
        manifest_entry = entries.get(MANIFEST_PATH)
        if manifest_entry is None:
            raise ReconcileBlocked("manifest_path_missing")
        manifest_files = []
        for path in sorted(set(entries) - {MANIFEST_PATH}):
            _mode, blob = entries[path]
            payload = _run_git(repo, "cat-file", "blob", blob).stdout
            manifest_files.append({
                "path": path,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            })
        if len(manifest_files) != proposal["expected_file_count"]:
            raise ReconcileBlocked("manifest_file_count_mismatch")
        readme_blob = entries.get("README.md")
        if readme_blob is None:
            raise ReconcileBlocked("readme_missing")
        readme = _run_git(repo, "cat-file", "blob", readme_blob[1]).stdout.decode("utf-8")
        version = re.search(r"^Versão:\s*(\S+)\s*$", readme, re.MULTILINE)
        if not version:
            raise ReconcileBlocked("readme_version_missing")
        manifest_payload = (
            json.dumps({
                "version": version.group(1),
                "generated_at": proposal["generated_at"],
                "files": manifest_files,
            }, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        canonical_payload = _canonical_manifest_payload(
            repo, index_env, entries, proposal["generated_at"],
        )
        if canonical_payload != manifest_payload:
            raise ReconcileBlocked("canonical_manifest_mismatch")
        manifest_blob = _run_git(repo, "hash-object", "-w", "--stdin", input_bytes=manifest_payload).stdout.decode().strip().lower()
        if manifest_blob != proposal["expected_manifest_blob"]:
            raise ReconcileBlocked("manifest_blob_mismatch")
        _run_git(
            repo, "update-index", "--add", "--cacheinfo", manifest_entry[0], manifest_blob,
            MANIFEST_PATH, env=index_env,
        )
        target_tree = _text(repo, "write-tree", env=index_env).lower()
        if target_tree != proposal["expected_tree"]:
            raise ReconcileBlocked("target_tree_mismatch")
    finally:
        index_path.unlink(missing_ok=True)
        Path(f"{index_path}.lock").unlink(missing_ok=True)

    commit_env = {
        "GIT_AUTHOR_NAME": IDENTITY_NAME,
        "GIT_AUTHOR_EMAIL": IDENTITY_EMAIL,
        "GIT_AUTHOR_DATE": proposal["commit_date"],
        "GIT_COMMITTER_NAME": IDENTITY_NAME,
        "GIT_COMMITTER_EMAIL": IDENTITY_EMAIL,
        "GIT_COMMITTER_DATE": proposal["commit_date"],
    }
    created = _run_git(
        repo, "commit-tree", target_tree, "-p", OLD_HEAD, "-p", proposal["main_sha"],
        env=commit_env, input_bytes=(MESSAGE + "\n").encode("utf-8"),
    ).stdout.decode().strip().lower()
    if created != proposal["expected_commit"]:
        raise ReconcileBlocked("target_commit_mismatch")
    if _parents(repo, created) != [OLD_HEAD, proposal["main_sha"]] or _tree(repo, created) != target_tree:
        raise ReconcileBlocked("target_commit_invalid")
    return {
        "manifest_blob": manifest_blob,
        "tree": target_tree,
        "commit": created,
        "file_count": len(manifest_files),
    }


def _preflight(repo: Path, proposal: Mapping[str, Any]) -> str:
    if socket.gethostname().casefold() != EXPECTED_HOST:
        raise ReconcileBlocked("host_mismatch")
    if str(repo.absolute()).casefold() != str(repo.resolve(strict=True)).casefold():
        raise ReconcileBlocked("repo_path_redirected")
    _assert_safe_git_config(repo)
    _validate_remote_configuration(repo)
    if _text(repo, "branch", "--show-current") != BRANCH:
        raise ReconcileBlocked("branch_mismatch")
    if _text(repo, "status", "--porcelain"):
        raise ReconcileBlocked("worktree_dirty")
    current = _sha(repo, "HEAD")
    if current not in {OLD_HEAD, proposal["expected_commit"]}:
        raise ReconcileBlocked("head_mismatch")
    if _remote_sha(repo, "main") != proposal["main_sha"]:
        raise ReconcileBlocked("remote_main_changed")
    if _sha(repo, "refs/remotes/origin/main") != proposal["main_sha"]:
        raise ReconcileBlocked("local_main_ref_changed")
    if _tree(repo, proposal["main_sha"]) != proposal["main_tree"]:
        raise ReconcileBlocked("main_tree_changed")
    if _text(repo, "merge-base", OLD_HEAD, proposal["main_sha"]).lower() != BASE_SHA:
        raise ReconcileBlocked("merge_base_changed")
    expected_pr_paths = set(OVERLAY_BLOBS) | {MANIFEST_PATH}
    if _changed_paths(repo, BASE_SHA, OLD_HEAD) != expected_pr_paths:
        raise ReconcileBlocked("pr_path_set_changed")
    for path, expected_blob in OVERLAY_BLOBS.items():
        if _sha(repo, f"{OLD_HEAD}:{path}") != expected_blob:
            raise ReconcileBlocked("reviewed_blob_changed")
    main_paths = _changed_paths(repo, BASE_SHA, proposal["main_sha"])
    for path in main_paths & set(OVERLAY_BLOBS):
        if _sha(repo, f"{proposal['main_sha']}:{path}") != OVERLAY_BLOBS[path]:
            raise ReconcileBlocked("main_overlay_conflict")
    remote_branch = _remote_sha(repo, BRANCH)
    allowed_remote = {OLD_HEAD, proposal["expected_commit"]} if current == proposal["expected_commit"] else {OLD_HEAD}
    if remote_branch not in allowed_remote:
        raise ReconcileBlocked("remote_branch_changed")
    return current


def reconcile(repo: Path, proposal_path: Path, proposal_sha256: str, correlation_id: str) -> dict[str, Any]:
    if not CORRELATION_RE.fullmatch(correlation_id):
        raise ReconcileBlocked("correlation_id_invalid")
    _assert_clean_git_environment()
    proposal = _load_proposal(proposal_path, proposal_sha256)
    _activate_git(proposal)
    current = _preflight(repo, proposal)
    candidate = _candidate(repo, proposal)
    if current == proposal["expected_commit"]:
        remote_branch = _remote_sha(repo, BRANCH)
        if remote_branch == OLD_HEAD:
            raise ReconcileBlocked("publication_pending")
        if remote_branch != proposal["expected_commit"]:
            raise ReconcileBlocked("remote_branch_changed")
        if _tree(repo, current) != proposal["expected_tree"] or _text(repo, "status", "--porcelain"):
            raise ReconcileBlocked("replay_state_invalid")
        return {
            "result": "ALREADY_RECONCILED",
            "head": current,
            "parents": [OLD_HEAD, proposal["main_sha"]],
            "proposal_sha256": proposal_sha256,
            "remote_compare_atomic": False,
            "correlation_id": correlation_id,
            **candidate,
        }

    if _remote_sha(repo, BRANCH) != OLD_HEAD or _remote_sha(repo, "main") != proposal["main_sha"]:
        raise ReconcileBlocked("remote_ref_changed_during_preflight")
    common_raw = Path(_text(repo, "rev-parse", "--git-common-dir"))
    common = common_raw if common_raw.is_absolute() else (repo / common_raw).resolve()
    hooks = Path(tempfile.mkdtemp(prefix="reqsys-pr185-empty-hooks-", dir=common))
    try:
        _plain_path(hooks, directory=True)
        if any(hooks.iterdir()):
            raise ReconcileBlocked("empty_hooks_path_unavailable")
        _run_git(
            repo, "-c", f"core.hooksPath={hooks}", "merge", "--ff-only", "--no-edit",
            proposal["expected_commit"],
        )
        if _sha(repo, "HEAD") != proposal["expected_commit"]:
            raise ReconcileBlocked("local_head_readback_failed")
        if _tree(repo, "HEAD") != proposal["expected_tree"] or _text(repo, "status", "--porcelain"):
            raise ReconcileBlocked("local_worktree_readback_failed")
        if _text(repo, "write-tree").lower() != proposal["expected_tree"]:
            raise ReconcileBlocked("local_index_readback_failed")
        if _remote_sha(repo, BRANCH) != OLD_HEAD:
            raise ReconcileBlocked("remote_branch_changed_before_push")
        if _remote_sha(repo, "main") != proposal["main_sha"]:
            raise ReconcileBlocked("remote_main_changed_before_push")
        _plain_path(hooks, directory=True)
        if any(hooks.iterdir()):
            raise ReconcileBlocked("empty_hooks_path_unavailable")
        push_error: BaseException | None = None
        try:
            _run_git(
                repo, "-c", f"core.hooksPath={hooks}", "push", "origin",
                f"HEAD:refs/heads/{BRANCH}",
            )
        except (ReconcileBlocked, subprocess.TimeoutExpired) as exc:
            push_error = exc
        remote_branch = _remote_sha(repo, BRANCH)
        remote_main = _remote_sha(repo, "main")
        if remote_main != proposal["main_sha"]:
            raise ReconcileBlocked("remote_main_changed_during_push")
        if remote_branch != proposal["expected_commit"]:
            if push_error is not None and remote_branch == OLD_HEAD:
                raise ReconcileBlocked("publication_incomplete")
            raise ReconcileBlocked("publication_outcome_unknown")
        return {
            "result": (
                "RECONCILED_AND_PUBLISHED_AFTER_READBACK"
                if push_error is not None
                else "RECONCILED_AND_PUBLISHED"
            ),
            "head": proposal["expected_commit"],
            "parents": [OLD_HEAD, proposal["main_sha"]],
            "proposal_sha256": proposal_sha256,
            "fast_forward": True,
            "force_push": False,
            "remote_compare_atomic": False,
            "push_had_local_error": push_error is not None,
            "correlation_id": correlation_id,
            **candidate,
        }
    finally:
        try:
            hooks.rmdir()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--proposal-sha256", required=True)
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--correlation-id", required=True)
    args = parser.parse_args()
    if args.confirm != CONFIRM:
        print(json.dumps({"result": "BLOCKED", "reason": "confirmation_invalid"}), file=sys.stderr)
        return 31
    try:
        print(json.dumps(reconcile(
            args.repo, args.proposal, args.proposal_sha256.lower(), args.correlation_id,
        ), sort_keys=True))
        return 0
    except (OSError, ReconcileBlocked, subprocess.SubprocessError) as exc:
        reason = str(exc)
        if not isinstance(exc, ReconcileBlocked) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", reason):
            reason = "reconciliation_failed"
        print(json.dumps({
            "result": "BLOCKED",
            "reason": reason,
            "remote_compare_atomic": False,
        }, sort_keys=True), file=sys.stderr)
        return 31


if __name__ == "__main__":
    raise SystemExit(main())

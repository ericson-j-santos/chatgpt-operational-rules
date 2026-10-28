#!/usr/bin/env python3
"""Atualiza o Command Gateway com preflight e recuperação reconciliável."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import stat
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import install_command_gateway_host as bootstrap

EXIT_HOST_UPDATE = 28
SUCCESS_RESULTS = {"HOST_UPDATE_OK", "HOST_UPDATE_ALREADY_CURRENT_VERIFIED"}
NOTERI_WSL_POLICY_SOURCE = "config/command-gateway.policy.noteri-wsl.json"
WINDOWS_POLICY_SOURCE = "config/command-gateway.policy.json"
DEFAULT_RUNTIME_MAP = dict(bootstrap.RUNTIME_MAP)


def runtime_map_for_profile(enabled: bool, *, platform: str, host: str) -> dict[str, str]:
    """Select a SHA-pinned policy without widening the default Windows bundle."""
    mapping = dict(DEFAULT_RUNTIME_MAP)
    if enabled:
        if platform != "nt" or host != "Noteri":
            raise bootstrap.HostBootstrapError("perfil WSL somente no host Noteri Windows")
        mapping[NOTERI_WSL_POLICY_SOURCE] = mapping.pop(WINDOWS_POLICY_SOURCE)
    return mapping


def verify_legacy_v1_with_original_map(
    install_root: Path, *, receipt_sha256: str, manifest_sha256: str,
) -> dict[str, Any]:
    """Never reinterpret historical v1 source paths as the new profile."""
    import noteri_gateway_legacy_v1_rebaseline as legacy

    selected_runtime_map = bootstrap.RUNTIME_MAP
    try:
        bootstrap.RUNTIME_MAP = dict(DEFAULT_RUNTIME_MAP)
        return legacy.verify_legacy_v1_current_state(
            install_root,
            expected_receipt_sha256=receipt_sha256,
            expected_manifest_sha256=manifest_sha256,
        )
    finally:
        bootstrap.RUNTIME_MAP = selected_runtime_map


class RuntimeReplaceabilityError(bootstrap.HostBootstrapError):
    """Falha fechada de preflight sem inferir a origem de um erro do SO."""

    def __init__(
        self,
        destination: str,
        category: str,
        detail: str,
        *,
        os_error: OSError | None = None,
    ) -> None:
        super().__init__(
            f"preflight de substituição falhou para {destination}: {detail}"
        )
        self.destination = destination
        self.category = category
        self.os_error = os_error


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _durable_atomic_json(path, payload)


def _exclusive_write(path: Path, payload: bytes) -> None:
    """Cria um recibo novo sem seguir nem substituir uma folha preexistente."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    completed = False
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("gravação exclusiva não avançou")
            view = view[written:]
        os.fsync(descriptor)
        completed = True
    finally:
        os.close(descriptor)
        if not completed:
            try:
                if path.is_file() and not path.is_symlink():
                    path.unlink()
            except OSError:
                pass


def _durable_atomic_json(path: Path, payload: dict[str, Any]) -> None:
    """Sincroniza o JSON antes do replace; não promete transação power-loss-safe."""
    temporary = path.with_name(path.name + f".tmp-{uuid.uuid4().hex}")
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    temporary_owned = False
    try:
        _exclusive_write(temporary, encoded)
        temporary_owned = True
        bootstrap.replace_file(temporary, path)
        temporary_owned = False
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if temporary_owned:
            _cleanup_probe(temporary)


class HostUpdateLock:
    """Lock de processo mantido por handle durante toda a transação."""

    def __init__(self, descriptor: int) -> None:
        self.descriptor = descriptor

    def close(self) -> None:
        if self.descriptor < 0:
            return
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(self.descriptor, 0, os.SEEK_SET)
                msvcrt.locking(self.descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self.descriptor)
            self.descriptor = -1


def acquire_update_lock(install_root: Path) -> HostUpdateLock:
    install_root.mkdir(parents=True, exist_ok=True)
    path = install_root / "update.lock"
    if path.is_symlink():
        raise bootstrap.HostBootstrapError("lock de atualização é symlink")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
            raise bootstrap.HostBootstrapError(
                "lock de atualização não é arquivo regular exclusivo"
            )
        if observed.st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise bootstrap.HostBootstrapError(
                "outra atualização do Command Gateway está ativa"
            ) from exc
        return HostUpdateLock(descriptor)
    except Exception:
        os.close(descriptor)
        raise


def _read_receipt(path: Path) -> dict[str, Any] | None:
    if path.is_symlink():
        raise bootstrap.HostBootstrapError("recibo de atualização é symlink")
    if not path.exists():
        return None
    if not path.is_file():
        raise bootstrap.HostBootstrapError("recibo de atualização não é arquivo regular")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise bootstrap.HostBootstrapError("recibo de atualização inválido") from exc
    if not isinstance(value, dict):
        raise bootstrap.HostBootstrapError("recibo de atualização não contém objeto")
    return value


def _read_transaction_journal(path: Path) -> dict[str, Any] | None:
    value = _read_receipt(path)
    if value is None:
        return None
    if (
        value.get("version") != 1
        or not isinstance(value.get("transaction_id"), str)
        or not value.get("transaction_id")
        or not isinstance(value.get("source_commit"), str)
        or not isinstance(value.get("backup_dir"), str)
        or not value.get("backup_dir")
    ):
        raise bootstrap.HostBootstrapError(
            "journal de atualização ausente ou inválido"
        )
    bootstrap.validate_commit(value["source_commit"])
    return value


def _remove_transaction_journal(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise bootstrap.HostBootstrapError(
            "journal de atualização não é arquivo regular"
        )
    path.unlink()
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _same_file_state(
    target: Path,
    sha256: str,
    size: int,
    mode: int | None = None,
    *,
    windows_state: dict[str, Any] | None = None,
    require_no_windows_streams: bool = False,
) -> bool:
    if target.is_symlink() or not target.is_file():
        return False
    try:
        observed = target.stat()
        content_matches = (
            observed.st_size == size
            and sha256_file(target) == sha256
            and (mode is None or stat.S_IMODE(observed.st_mode) == mode)
        )
        if not content_matches or not bootstrap._windows_metadata_required():
            return content_matches
        actual_windows_state = bootstrap._windows_persisted_file_state(target)
        if windows_state is not None:
            bootstrap._validate_windows_persisted_state(
                windows_state, str(target)
            )
            return bootstrap._windows_persisted_state_equivalent(
                windows_state, actual_windows_state
            )
        return not require_no_windows_streams or not actual_windows_state["streams"]
    except OSError:
        return False


def reconcile_runtime(install_root: Path, backup_dir: Path) -> dict[str, Any]:
    """Classifica o estado observado contra os estados anterior e desejado."""
    snapshot = bootstrap._load_runtime_rollback(install_root, backup_dir)
    entries: dict[str, dict[str, Any]] = {}
    all_previous = True
    all_desired = True
    for destination, entry in snapshot["entries"].items():
        target = bootstrap._managed_target(install_root, destination)
        expected_windows_state = (
            entry.get("previous_windows_state") if entry["existed"] else None
        )
        previous = (
            _same_file_state(
                target,
                entry["previous_sha256"],
                entry["previous_size"],
                entry["previous_mode"],
                windows_state=expected_windows_state,
            )
            if entry["existed"]
            else not target.exists() and not target.is_symlink()
        )
        desired = _same_file_state(
            target,
            entry["desired_sha256"],
            entry["desired_size"],
            windows_state=expected_windows_state,
            require_no_windows_streams=not entry["existed"],
        )
        all_previous = all_previous and previous
        all_desired = all_desired and desired
        if previous and desired:
            state = "previous_and_desired"
        elif previous:
            state = "previous"
        elif desired:
            state = "desired"
        elif target.is_symlink():
            state = "invalid_symlink"
        elif not target.exists():
            state = "missing"
        elif not target.is_file():
            state = "invalid_non_file"
        else:
            state = "divergent"
        entries[destination] = {
            "state": state,
            "previous": previous,
            "desired": desired,
        }
    if all_previous and all_desired:
        overall = "previous_and_desired"
    elif all_previous:
        overall = "previous"
    elif all_desired:
        overall = "desired"
    else:
        overall = "mixed_or_divergent"
    return {
        "overall": overall,
        "previous_state": all_previous,
        "desired_state": all_desired,
        "entries": entries,
    }


def desired_runtime_state(
    bundle_dir: Path,
    install_root: Path,
    expected_files: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Comprova o bundle instalado sem confiar em recibo anterior."""
    _, index = bootstrap.verify_bundle(bundle_dir)
    files: dict[str, dict[str, Any]] = {}
    matched = True
    for source, destination in bootstrap.RUNTIME_MAP.items():
        target = bootstrap._managed_target(install_root, destination)
        expected = index[source]
        expected_windows_state = None
        if bootstrap._windows_metadata_required():
            if not isinstance(expected_files, dict):
                raise bootstrap.HostBootstrapError(
                    "recibo Windows não possui metadados reconciliáveis"
                )
            expected_file = expected_files.get(destination)
            if not isinstance(expected_file, dict):
                raise bootstrap.HostBootstrapError(
                    f"recibo Windows não descreve {destination}"
                )
            expected_windows_state = expected_file.get("windows_state")
            bootstrap._validate_windows_persisted_state(
                expected_windows_state, destination
            )
        current = _same_file_state(
            target,
            str(expected["sha256"]).lower(),
            int(expected["size"]),
            windows_state=expected_windows_state,
        )
        matched = matched and current
        observed_file: dict[str, Any] = {
            "matched": current,
            "sha256": sha256_file(target) if current else None,
            "size": target.stat().st_size if current else None,
        }
        if current and expected_windows_state is not None:
            observed_file["windows_state"] = (
                bootstrap._windows_persisted_file_state(target)
            )
        files[destination] = observed_file
    return {"matched": matched, "files": files}


def _cleanup_probe(path: Path) -> str | None:
    try:
        if path.is_symlink():
            return f"probe inesperadamente virou symlink: {path.name}"
        if path.exists():
            try:
                path.chmod(stat.S_IREAD | stat.S_IWRITE)
            except OSError:
                pass
            path.unlink()
    except OSError as exc:
        return f"limpeza de probe falhou ({type(exc).__name__}: {exc})"
    return None


def _target_is_read_only(target: Path) -> bool:
    observed = target.stat()
    if os.name == "nt":
        attributes = getattr(observed, "st_file_attributes", None)
        read_only_attribute = getattr(stat, "FILE_ATTRIBUTE_READONLY", 0x1)
        if attributes is not None:
            return bool(attributes & read_only_attribute)
    return not bool(stat.S_IMODE(observed.st_mode) & stat.S_IWUSR)


def _windows_delete_access_probe(target: Path, destination: str) -> None:
    """Abre o alvo com DELETE sem alterar conteúdo, ACL, ADS ou identidade."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    handle = create_file(
        str(target),
        0x00010000,  # DELETE
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,  # OPEN_EXISTING
        0x00000080,  # FILE_ATTRIBUTE_NORMAL
        None,
    )
    invalid_handle = wintypes.HANDLE(-1).value
    if handle == invalid_handle:
        error_code = ctypes.get_last_error()
        error = ctypes.WinError(error_code)
        raise RuntimeReplaceabilityError(
            destination,
            "delete_access_os_error",
            f"{type(error).__name__}: {error}",
            os_error=error,
        ) from error
    if not close_handle(handle):
        error_code = ctypes.get_last_error()
        error = ctypes.WinError(error_code)
        raise RuntimeReplaceabilityError(
            destination,
            "delete_probe_close_error",
            f"{type(error).__name__}: {error}",
            os_error=error,
        ) from error


def preflight_runtime_replaceability(install_root: Path, backup_dir: Path) -> dict[str, Any]:
    """Exercita a primitiva real de replace antes de escrever bytes desejados.

    A falha de ``os.replace`` pode decorrer de sharing mode, ACL, antivírus ou
    outra interferência. O preflight registra o erro do SO e não escolhe uma
    causa sem evidência. Arquivo explicitamente read-only é diagnosticado antes.
    """
    snapshot = bootstrap._load_runtime_rollback(install_root, backup_dir)
    initial = reconcile_runtime(install_root, backup_dir)
    if not initial["previous_state"]:
        raise bootstrap.HostBootstrapError(
            "estado mudou entre snapshot e preflight de substituição"
        )
    checked: list[str] = []
    for destination, entry in snapshot["entries"].items():
        target = bootstrap._managed_target(install_root, destination)
        first = target.with_name(target.name + f".replace-probe-{uuid.uuid4().hex}")
        second = target.with_name(target.name + f".replace-probe-{uuid.uuid4().hex}")
        first_owned = False
        second_owned = False
        failure: RuntimeReplaceabilityError | None = None
        cleanup_errors: list[str] = []
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if entry["existed"]:
                if _target_is_read_only(target):
                    raise RuntimeReplaceabilityError(
                        destination,
                        "read_only",
                        "arquivo gerenciado está marcado como read-only",
                    )
                _windows_delete_access_probe(target, destination)
            _exclusive_write(first, b"reqsys-replaceability-probe\n")
            first_owned = True
            _exclusive_write(second, b"reqsys-replaceability-destination\n")
            second_owned = True
            try:
                bootstrap.replace_file(first, second)
                first_owned = False
            except OSError as exc:
                raise RuntimeReplaceabilityError(
                    destination,
                    "replace_os_error",
                    f"{type(exc).__name__}: {exc}",
                    os_error=exc,
                ) from exc
            second.unlink()
            second_owned = False
            checked.append(destination)
        except RuntimeReplaceabilityError as exc:
            failure = exc
        except OSError as exc:
            failure = RuntimeReplaceabilityError(
                destination,
                "filesystem_os_error",
                f"{type(exc).__name__}: {exc}",
                os_error=exc,
            )
        finally:
            owned_probes = []
            if first_owned:
                owned_probes.append(first)
            if second_owned:
                owned_probes.append(second)
            for probe in owned_probes:
                cleanup_error = _cleanup_probe(probe)
                if cleanup_error:
                    cleanup_errors.append(cleanup_error)
        if failure or cleanup_errors:
            if failure and cleanup_errors:
                raise RuntimeReplaceabilityError(
                    destination,
                    failure.category,
                    f"{failure}; {'; '.join(cleanup_errors)}",
                    os_error=failure.os_error,
                ) from failure
            if failure:
                raise failure
            raise RuntimeReplaceabilityError(
                destination,
                "probe_cleanup_failed",
                "; ".join(cleanup_errors),
            )
    final = reconcile_runtime(install_root, backup_dir)
    if not final["previous_state"]:
        raise bootstrap.HostBootstrapError(
            "preflight não preservou o estado anterior do runtime"
        )
    return {"checked": checked, "reconciliation": final}


def restore_backup(install_root: Path, backup_dir: Path | None) -> None:
    if backup_dir is None:
        return
    bootstrap.restore_runtime_rollback(install_root, backup_dir)


def _failure_metadata(exc: Exception) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "type": type(exc).__name__,
        "cause_determined": False,
        "retry_policy": "requires_changed_precondition_and_new_transaction",
    }
    if isinstance(exc, RuntimeReplaceabilityError):
        metadata.update(
            {
                "destination": exc.destination,
                "category": exc.category,
                "candidate_causes": (
                    ["read_only_attribute"]
                    if exc.category == "read_only"
                    else [
                        "open_handle_without_delete_share",
                        "acl_or_directory_permission",
                        "security_or_antivirus_interference",
                        "other_filesystem_interference",
                    ]
                ),
            }
        )
        if exc.category == "read_only":
            metadata["cause_determined"] = True
        if exc.os_error is not None:
            metadata["os_error"] = {
                "errno": exc.os_error.errno,
                "winerror": getattr(exc.os_error, "winerror", None),
                "strerror": exc.os_error.strerror,
            }
    return metadata


def _safe_reconciliation(
    install_root: Path, backup_dir: Path | None
) -> dict[str, Any] | None:
    if backup_dir is None:
        return None
    try:
        return reconcile_runtime(install_root, backup_dir)
    except Exception as exc:
        return {
            "overall": "unavailable",
            "previous_state": False,
            "desired_state": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _persist_blocked_receipt(
    receipt_path: Path,
    install_root: Path,
    transaction_id: str,
    payload: dict[str, Any],
    *,
    allow_primary: bool,
) -> dict[str, Any]:
    primary_error: Exception | None = None
    if allow_primary:
        primary = dict(payload)
        primary["receipt_path"] = str(receipt_path)
        try:
            atomic_json(receipt_path, primary)
            return primary
        except Exception as exc:
            primary_error = exc
    fallback_path = install_root / f"update-recovery-{transaction_id}.json"
    fallback = dict(payload)
    fallback["receipt_path"] = str(fallback_path)
    if primary_error is not None:
        fallback["primary_receipt_error"] = (
            f"{type(primary_error).__name__}: {primary_error}"
        )
    elif not allow_primary:
        fallback["primary_receipt_error"] = "lock da transação não adquirido"
    try:
        _exclusive_write(
            fallback_path,
            (json.dumps(fallback, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        )
    except Exception as fallback_exc:
        fallback["fallback_receipt_error"] = (
            f"{type(fallback_exc).__name__}: {fallback_exc}"
        )
    return fallback



def readback_existing_legacy_v2_stage(
    install_root: Path, *, source_commit: str,
    legacy_receipt_sha256: str, legacy_manifest_sha256: str,
) -> dict[str, Any] | None:
    """Idempotent, read-only verification of a previously completed stage.

    A partial or divergent stage blocks, rather than silently creating more
    snapshots under the same historical receipt.
    """
    if not install_root.is_dir() or install_root.is_symlink():
        raise bootstrap.HostBootstrapError("raiz de stage legado inválida")
    candidates = [
        p for p in install_root.iterdir()
        if p.name.startswith("backup-") and (
            (p / "legacy-stage-receipt.json").exists()
            or (p / "legacy-update-receipt.json").exists()
        )
    ]
    if len(candidates) > 16:
        raise bootstrap.HostBootstrapError("histórico de stages legados ambíguo")
    verified: list[dict[str, Any]] = []
    for folder in candidates:
        if folder.is_symlink() or not folder.is_dir():
            raise bootstrap.HostBootstrapError("diretório de stage legado inválido")
        stage_file = folder / "legacy-stage-receipt.json"
        archive = folder / "legacy-update-receipt.json"
        if stage_file.is_symlink() or archive.is_symlink():
            raise bootstrap.HostBootstrapError("recibo de stage legado é reparse")
        if not stage_file.is_file() or not archive.is_file():
            raise bootstrap.HostBootstrapError("stage legado incompleto")
        receipt = _read_receipt(stage_file)
        if (
            receipt is None
            or receipt.get("result") != "HOST_LEGACY_V2_STAGED_ONLY"
            or receipt.get("source_commit") != source_commit
            or receipt.get("legacy_receipt_sha256") != legacy_receipt_sha256
            or receipt.get("legacy_manifest_sha256") != legacy_manifest_sha256
            or receipt.get("backup_dir") != str(folder)
            or receipt.get("historical_windows_metadata_verified") is not False
            or receipt.get("current_windows_metadata_snapshotted") is not True
            or receipt.get("installation_performed") is not False
            or receipt.get("managed_runtime_changed") is not False
            or receipt.get("old_receipt_archived") is not True
            or sha256_file(archive) != legacy_receipt_sha256
            or sha256_file(folder / "rollback-manifest.json")
                != receipt.get("v2_manifest_sha256")
        ):
            raise bootstrap.HostBootstrapError("stage legado divergente")
        snapshot = bootstrap._load_runtime_rollback(install_root, folder)
        if snapshot.get("version") != bootstrap.ROLLBACK_MANIFEST_VERSION:
            raise bootstrap.HostBootstrapError("stage legado não contém v2 íntegro")
        if not reconcile_runtime(install_root, folder)["previous_state"]:
            raise bootstrap.HostBootstrapError("stage legado divergiu do host atual")
        verified.append(receipt)
    if len(verified) > 1:
        raise bootstrap.HostBootstrapError("mais de um stage legado confirmado")
    return verified[0] if verified else None

def main() -> int:
    parser = argparse.ArgumentParser(description="Atualização governada do runtime Command Gateway")
    parser.add_argument("--commit", required=True, help="SHA completo aprovado do repositório canônico")
    parser.add_argument("--expected-self-sha256", required=True, help="SHA-256 esperado deste updater")
    parser.add_argument("--install-root", type=Path, default=None)
    parser.add_argument("--work-root", type=Path, default=Path(r"C:\dev\chatgpt-workers"))
    parser.add_argument("--noteri-legacy-v1-rebaseline", action="store_true")
    parser.add_argument("--noteri-legacy-v1-stage-only", action="store_true")
    parser.add_argument("--expected-legacy-receipt-sha256")
    parser.add_argument("--expected-legacy-manifest-sha256")
    parser.add_argument("--noteri-wsl-inventory-profile", action="store_true")
    ns = parser.parse_args()

    install_root = ns.install_root or bootstrap.default_install_root()
    receipt_path = install_root / "update-receipt.json"
    journal_path = install_root / "update-transaction.json"
    transaction_id = uuid.uuid4().hex
    update_lock: HostUpdateLock | None = None
    backup_dir: Path | None = None
    commit: str | None = None
    phase = "self_verification"
    mutation_started = False
    rollback_attempted = False
    rollback_succeeded: bool | None = None
    rollback_error: str | None = None
    reconciliation: dict[str, Any] | None = None
    journal_recovered = False
    legacy_readback = None
    legacy_receipt_archived = False
    try:
        legacy_requested = ns.noteri_legacy_v1_rebaseline
        if ns.noteri_legacy_v1_stage_only and not legacy_requested:
            raise bootstrap.HostBootstrapError(
                "snapshot v2 stage-only exige rebaseline Noteri explicitamente autorizado"
            )
        legacy_pins = (ns.expected_legacy_receipt_sha256,
                       ns.expected_legacy_manifest_sha256)
        if (legacy_requested and any(value is None for value in legacy_pins)
                or not legacy_requested and any(value is not None for value in legacy_pins)):
            raise bootstrap.HostBootstrapError(
                "rebaseline Noteri exige flag explícita e dois hashes completos"
            )
        expected = ns.expected_self_sha256.strip().lower()
        actual = sha256_file(Path(__file__).resolve())
        if len(expected) != 64 or actual != expected:
            raise bootstrap.HostBootstrapError("SHA-256 do updater divergente")
        commit = bootstrap.validate_commit(ns.commit)
        if ns.noteri_wsl_inventory_profile and ns.noteri_legacy_v1_stage_only:
            raise bootstrap.HostBootstrapError(
                "perfil WSL exige atualização governada, não stage-only"
            )
        bootstrap.RUNTIME_MAP = runtime_map_for_profile(
            ns.noteri_wsl_inventory_profile,
            platform=os.name,
            host=socket.gethostname(),
        )

        phase = "acquire_update_lock"
        update_lock = acquire_update_lock(install_root)
        phase = "recover_transaction_journal"
        journal = _read_transaction_journal(journal_path)
        if journal is not None:
            if legacy_requested:
                raise bootstrap.HostBootstrapError(
                    "rebaseline legado recusado com journal ainda presente"
                )
            journal_recovered = True
            backup_dir = Path(journal["backup_dir"])
            reconciliation = reconcile_runtime(install_root, backup_dir)
            if not reconciliation["previous_state"]:
                rollback_attempted = True
                try:
                    restore_backup(install_root, backup_dir)
                except Exception as exc:
                    rollback_error = str(exc)
                reconciliation = reconcile_runtime(install_root, backup_dir)
                rollback_succeeded = reconciliation["previous_state"]
                if not rollback_succeeded:
                    raise bootstrap.HostBootstrapError(
                        "journal pendente não pôde ser reconciliado"
                    )
            _remove_transaction_journal(journal_path)
            backup_dir = None

        existing_gateway = install_root / "bin" / "command_gateway.py"
        existing_policy = install_root / "config" / "policy.json"
        if not existing_gateway.is_file() or not existing_policy.is_file():
            raise bootstrap.HostBootstrapError("runtime existente não comprovado; use bootstrap inicial")

        phase = "read_previous_receipt"
        previous_receipt = _read_receipt(receipt_path)
        if legacy_requested:
            if (journal_recovered or not previous_receipt
                    or previous_receipt.get("result") != "HOST_UPDATE_BLOCKED"):
                raise bootstrap.HostBootstrapError(
                    "rebaseline legado exige recibo bloqueado sem journal"
                )
            # Only for exact Noteri Win32 + pinned original v1 receipt/manifest.
            # The historical v1 is NEVER treated as Windows metadata evidence.
            phase = "verify_legacy_v1_live_state"
            legacy_readback = verify_legacy_v1_with_original_map(
                install_root,
                receipt_sha256=ns.expected_legacy_receipt_sha256,
                manifest_sha256=ns.expected_legacy_manifest_sha256,
            )
        else:
            if (
                not journal_recovered
                and previous_receipt
                and previous_receipt.get("result") == "HOST_UPDATE_BLOCKED"
            ):
                previous_backup = previous_receipt.get("backup_dir")
                previous_mutation = previous_receipt.get("mutation_started")
                if previous_backup and previous_mutation is not False:
                    phase = "replay_reconciliation"
                    backup_dir = Path(str(previous_backup))
                    reconciliation = reconcile_runtime(install_root, backup_dir)
                    if not reconciliation["previous_state"]:
                        rollback_attempted = True
                        try:
                            restore_backup(install_root, backup_dir)
                        except Exception as exc:
                            rollback_error = str(exc)
                        reconciliation = reconcile_runtime(install_root, backup_dir)
                        rollback_succeeded = reconciliation["previous_state"]
                        if not rollback_succeeded:
                            raise bootstrap.HostBootstrapError(
                                "replay não conseguiu reconciliar a tentativa anterior"
                            )
                    backup_dir = None
                elif previous_mutation is not False:
                    raise bootstrap.HostBootstrapError(
                        "tentativa anterior mutou o runtime sem snapshot recuperável"
                    )

        if ns.noteri_legacy_v1_stage_only:
            phase = "legacy_v2_existing_stage_readback"
            existing_stage = readback_existing_legacy_v2_stage(
                install_root, source_commit=commit,
                legacy_receipt_sha256=ns.expected_legacy_receipt_sha256,
                legacy_manifest_sha256=ns.expected_legacy_manifest_sha256,
            )
            if existing_stage is not None:
                print(json.dumps({
                    "result": "HOST_LEGACY_V2_ALREADY_STAGED_VERIFIED",
                    "v2_manifest_sha256": existing_stage["v2_manifest_sha256"],
                    "replacement_preflight": existing_stage["replacement_preflight"],
                    "replaceability_proved": existing_stage["replaceability_proved"],
                    "installation_performed": False,
                    "managed_runtime_changed": False,
                }, sort_keys=True))
                return 0

        with tempfile.TemporaryDirectory(prefix="reqsys-host-update-") as tmp:
            bundle = Path(tmp)
            phase = "download_bundle"
            bootstrap.download_bundle(commit, bundle)

            if (
                not journal_recovered
                and previous_receipt
                and previous_receipt.get("result") in SUCCESS_RESULTS
                and previous_receipt.get("source_commit") == commit
                and previous_receipt.get("noteri_wsl_inventory_profile", False)
                == ns.noteri_wsl_inventory_profile
            ):
                phase = "idempotent_readback"
                desired = desired_runtime_state(
                    bundle,
                    install_root,
                    previous_receipt.get("files"),
                )
                if not desired["matched"]:
                    raise bootstrap.HostBootstrapError(
                        "recibo de sucesso existe, mas o runtime instalado diverge"
                    )
                git_executable, git_source = bootstrap.resolve_git(install_root)
                validation_repo, validation_head = bootstrap.prepare_validation_repo(
                    ns.work_root, commit, git_executable=git_executable
                )
                receipt = {
                    "result": "HOST_UPDATE_ALREADY_CURRENT_VERIFIED",
                    "transaction_id": transaction_id,
                    "source_commit": commit,
                    "noteri_wsl_inventory_profile": ns.noteri_wsl_inventory_profile,
                    "verified_at": utc_now(),
                    "install_root": str(install_root),
                    "backup_dir": previous_receipt.get("backup_dir"),
                    "git_source": git_source,
                    "validation_repo": str(validation_repo),
                    "validation_head": validation_head,
                    "files": desired["files"],
                    "mutation_started": False,
                }
                phase = "write_success_receipt"
                atomic_json(receipt_path, receipt)
                print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
                return 0

            phase = "prepare_snapshot"
            backup_dir = bootstrap.prepare_runtime_rollback(bundle, install_root)
            if legacy_readback is not None:
                # This is a NEW v2 baseline of the current host, not a forged
                # v2 version of the legacy backup. Preserve the old receipt
                # exactly before any write to the managed runtime.
                phase = "verify_new_v2_snapshot"
                snapshot = bootstrap._load_runtime_rollback(install_root, backup_dir)
                if snapshot.get("version") != bootstrap.ROLLBACK_MANIFEST_VERSION:
                    raise bootstrap.HostBootstrapError(
                        "novo snapshot v2 de rollback não foi comprovado"
                    )
                phase = "archive_original_legacy_receipt"
                original = receipt_path.read_bytes()
                if hashlib.sha256(original).hexdigest() != ns.expected_legacy_receipt_sha256:
                    raise bootstrap.HostBootstrapError(
                        "recibo histórico mudou antes de ser preservado"
                    )
                archive = backup_dir / "legacy-update-receipt.json"
                _exclusive_write(archive, original)
                if sha256_file(archive) != ns.expected_legacy_receipt_sha256:
                    raise bootstrap.HostBootstrapError(
                        "readback da cópia histórica falhou"
                    )
                legacy_receipt_archived = True
            if ns.noteri_legacy_v1_stage_only:
                phase = "legacy_v2_stage_only_preflight"
                replace_category = "ok"
                try:
                    preflight_runtime_replaceability(install_root, backup_dir)
                except RuntimeReplaceabilityError as exc:
                    # The original six managed files remain unchanged;
                    # retain the authentic v2 baseline but do not install.
                    replace_category = exc.category
                current_state = reconcile_runtime(install_root, backup_dir)
                if not current_state["previous_state"]:
                    raise bootstrap.HostBootstrapError(
                        "snapshot v2 divergiu após qualificação de substituição"
                    )
                if sha256_file(receipt_path) != ns.expected_legacy_receipt_sha256:
                    raise bootstrap.HostBootstrapError(
                        "recibo histórico divergiu antes da etapa somente backup"
                    )
                staged = {
                    "result": "HOST_LEGACY_V2_STAGED_ONLY",
                    "source_commit": commit,
                    "source_head": commit,
                    "legacy_receipt_sha256": ns.expected_legacy_receipt_sha256,
                    "legacy_manifest_sha256": ns.expected_legacy_manifest_sha256,
                    "v2_manifest_sha256": sha256_file(
                        backup_dir / "rollback-manifest.json"
                    ),
                    "backup_dir": str(backup_dir),
                    "managed_file_count": len(current_state["entries"]),
                    "historical_windows_metadata_verified": False,
                    "current_windows_metadata_snapshotted": True,
                    "old_receipt_archived": True,
                    "replacement_preflight": replace_category,
                    "replaceability_proved": replace_category == "ok",
                    "managed_runtime_changed": False,
                    "installation_performed": False,
                    "wsl_invoked": False,
                }
                phase = "persist_stage_only_receipt"
                _durable_atomic_json(
                    backup_dir / "legacy-stage-receipt.json", staged
                )
                reread = _read_receipt(backup_dir / "legacy-stage-receipt.json")
                if reread != staged:
                    raise bootstrap.HostBootstrapError(
                        "recibo de stage-only não foi confirmado independentemente"
                    )
                # No managed files or update-receipt mutation. Replays verify
                # this exact stage by independent snapshot, metadata and
                # archived historical-receipt readback.
                print(json.dumps({
                    "result": staged["result"],
                    "v2_manifest_sha256": staged["v2_manifest_sha256"],
                    "legacy_receipt_sha256": staged["legacy_receipt_sha256"],
                    "replacement_preflight": staged["replacement_preflight"],
                    "replaceability_proved": staged["replaceability_proved"],
                    "installation_performed": False,
                    "managed_runtime_changed": False,
                }, sort_keys=True))
                return 0
            phase = "replaceability_preflight"
            preflight_runtime_replaceability(install_root, backup_dir)
            reconciliation = reconcile_runtime(install_root, backup_dir)
            if not reconciliation["previous_state"]:
                raise bootstrap.HostBootstrapError(
                    "estado anterior não comprovado imediatamente antes da instalação"
                )

            phase = "write_transaction_journal"
            _durable_atomic_json(
                journal_path,
                {
                    "version": 1,
                    "transaction_id": transaction_id,
                    "source_commit": commit,
                    "backup_dir": str(backup_dir),
                    "prepared_at": utc_now(),
                },
            )
            phase = "install_runtime"
            mutation_started = True
            result = bootstrap.install_bundle(
                bundle,
                install_root,
                ns.work_root,
                commit,
                rollback_dir=backup_dir,
                rollback_on_error=False,
                write_receipt=False,
            )
            reconciliation = reconcile_runtime(install_root, backup_dir)
            if not reconciliation["desired_state"]:
                raise bootstrap.HostBootstrapError(
                    "estado desejado não comprovado após instalação"
                )

        phase = "validate_control_plane"
        git_executable, git_source = bootstrap.resolve_git(install_root)
        validation_repo, validation_head = bootstrap.prepare_validation_repo(
            ns.work_root, commit, git_executable=git_executable
        )
        receipt = {
            "result": "HOST_UPDATE_OK",
            "transaction_id": transaction_id,
            "source_commit": commit,
            "noteri_wsl_inventory_profile": ns.noteri_wsl_inventory_profile,
            "rules_version": result.get("rules_version"),
            "updated_at": utc_now(),
            "host": result.get("host"),
            "install_root": str(install_root),
            "backup_dir": str(backup_dir) if backup_dir else None,
            "git_source": git_source,
            "validation_repo": str(validation_repo),
            "validation_head": validation_head,
            "files": result.get("files", {}),
            "mutation_started": True,
            "reconciliation": reconciliation,
            "legacy_v1_rebaseline": (
                {"admitted": True, "historical_metadata_verified": False,
                 "original_receipt_archived": legacy_receipt_archived}
                if legacy_readback is not None else None
            ),
        }
        phase = "write_success_receipt"
        atomic_json(receipt_path, receipt)
        phase = "finalize_transaction_journal"
        _remove_transaction_journal(journal_path)
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        if mutation_started and backup_dir is not None:
            rollback_attempted = True
            try:
                restore_backup(install_root, backup_dir)
            except Exception as rollback_exc:
                rollback_error = str(rollback_exc)
            reconciliation = _safe_reconciliation(install_root, backup_dir)
            rollback_succeeded = bool(
                reconciliation and reconciliation.get("previous_state")
            )
            if not rollback_succeeded and rollback_error is None:
                rollback_error = "rollback retornou sem restaurar o estado anterior"
        elif backup_dir is not None:
            reconciliation = _safe_reconciliation(install_root, backup_dir)

        blocked = {
            "result": "HOST_UPDATE_BLOCKED",
            "gateway_exit_code": EXIT_HOST_UPDATE,
            "transaction_id": transaction_id,
            "source_commit": commit,
            "blocked_at": utc_now(),
            "phase": phase,
            "error": str(exc),
            "failure": _failure_metadata(exc),
            "backup_dir": str(backup_dir) if backup_dir else None,
            "mutation_started": mutation_started,
            "rollback_attempted": rollback_attempted,
            "rollback_succeeded": rollback_succeeded,
            "rollback_error": rollback_error,
            "reconciliation": reconciliation,
            "legacy_v1_rebaseline": (
                {"admitted": True, "historical_metadata_verified": False,
                 "original_receipt_archived": legacy_receipt_archived}
                if legacy_readback is not None else None
            ),
        }
        if ns.noteri_legacy_v1_stage_only:
            # Stage-only is never allowed to overwrite the historical receipt,
            # even if an error happens after the new snapshot is created.
            blocked["result"] = "HOST_LEGACY_V2_STAGE_ONLY_BLOCKED"
            blocked["original_receipt_not_overwritten"] = True
            print(json.dumps(blocked, ensure_ascii=False, sort_keys=True), file=sys.stderr)
            return EXIT_HOST_UPDATE
        if ns.noteri_legacy_v1_rebaseline and not legacy_receipt_archived:
            # Never clobber the only historical failed receipt when the
            # qualifying readback or full Windows v2 snapshot was not proved.
            blocked["result"] = "HOST_UPDATE_LEGACY_PREFLIGHT_BLOCKED"
            blocked["original_receipt_not_overwritten"] = True
            print(json.dumps(blocked, ensure_ascii=False, sort_keys=True), file=sys.stderr)
            return EXIT_HOST_UPDATE
        persisted = _persist_blocked_receipt(
            receipt_path,
            install_root,
            transaction_id,
            blocked,
            allow_primary=update_lock is not None,
        )
        print(json.dumps(persisted, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return EXIT_HOST_UPDATE
    finally:
        bootstrap.RUNTIME_MAP = dict(DEFAULT_RUNTIME_MAP)
        if update_lock is not None:
            try:
                update_lock.close()
            except OSError as exc:
                warning = {
                    "result": "HOST_UPDATE_LOCK_RELEASE_WARNING",
                    "transaction_id": transaction_id,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                print(json.dumps(warning, ensure_ascii=False, sort_keys=True), file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())

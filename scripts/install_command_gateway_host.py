#!/usr/bin/env python3
"""Bootstrap mínimo e verificável para instalar o Command Gateway em host novo."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

REPOSITORY = "ericson-j-santos/chatgpt-operational-rules"
RAW_ROOT = f"https://raw.githubusercontent.com/{REPOSITORY}"
EXIT_HOST_BOOTSTRAP = 27
MAX_DOWNLOAD_BYTES = 4 * 1024 * 1024
COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
RUNTIME_MAP = {
    "scripts/command_gateway.py": "bin/command_gateway.py",
    "scripts/host_health_check.py": "bin/host_health_check.py",
    "scripts/session_bootstrap.py": "bin/session_bootstrap.py",
    "scripts/session_preflight.py": "bin/session_preflight.py",
    "scripts/session_launcher.py": "bin/session_launcher.py",
    "scripts/runner_version_preflight.py": "bin/runner_version_preflight.py",
    "config/command-gateway.policy.json": "config/policy.json",
}
MINGIT_VERSION = "2.55.0.5"
MINGIT_URL = (
    "https://github.com/git-for-windows/git/releases/download/"
    f"v{MINGIT_VERSION}.windows.5/MinGit-{MINGIT_VERSION}-64-bit.zip"
)
MINGIT_SHA256 = "56d7b226b7693196cfc71fef26568f536c4a021ab6c37ff2db4287bed908e96e"
MINGIT_SIZE = 38_989_688
MAX_MINGIT_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_MINGIT_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_MINGIT_MEMBERS = 10_000
ROLLBACK_MANIFEST_VERSION = 2
WINDOWS_ROLLBACK_METADATA_CONTRACT = "windows-owner-group-dacl-ads-v1"
POSIX_ROLLBACK_METADATA_CONTRACT = "posix-mode-v1"
MAX_WINDOWS_ROLLBACK_STREAMS = 128
MAX_WINDOWS_ROLLBACK_ACES = 2_048


class HostBootstrapError(RuntimeError):
    pass


class WindowsReplaceError(OSError):
    """Falha sanitizada e localizável da primitiva de replace no Windows."""

    def __init__(self, operation: str, cause: BaseException) -> None:
        self.operation = operation
        self.os_error = cause if isinstance(cause, OSError) else None
        errno = self.os_error.errno if self.os_error is not None else None
        super().__init__(
            errno,
            f"falha Windows durante {operation}: {type(cause).__name__}",
        )


class RuntimeInstallError(HostBootstrapError):
    """Vincula uma falha de instalação ao destino gerenciado allowlisted."""

    def __init__(
        self,
        destination: str,
        operation: str,
        cause: BaseException,
    ) -> None:
        self.destination = destination
        self.operation = operation
        if isinstance(cause, WindowsReplaceError):
            self.os_error = cause.os_error
        else:
            self.os_error = cause if isinstance(cause, OSError) else None
        super().__init__(
            "instalação do runtime bloqueada em "
            f"{destination} durante {operation}: {type(cause).__name__}"
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def emit_json(payload: dict[str, Any], *, file=None) -> None:
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True), file=file)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_commit(value: str) -> str:
    if not COMMIT_RE.fullmatch(value):
        raise HostBootstrapError("commit deve ser SHA completo de 40 caracteres hexadecimais")
    return value.lower()


def validate_sha256(value: str) -> str:
    if not SHA256_RE.fullmatch(value):
        raise HostBootstrapError("SHA-256 esperado inválido")
    return value.lower()


def verify_self(expected_sha256: str) -> None:
    expected = validate_sha256(expected_sha256)
    actual = sha256_file(Path(__file__).resolve())
    if actual != expected:
        raise HostBootstrapError(f"integridade do instalador inválida: esperado={expected} atual={actual}")


def download_bytes(url: str, timeout: int = 30) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "ReqSys-Host-Bootstrap/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(payload) > MAX_DOWNLOAD_BYTES:
        raise HostBootstrapError(f"download excede limite: {url}")
    return payload


def verify_file(path: Path, expected_sha256: str, expected_size: int) -> None:
    expected = validate_sha256(expected_sha256)
    if not path.is_file():
        raise HostBootstrapError(f"arquivo ausente para verificação: {path}")
    actual_size = path.stat().st_size
    actual_hash = sha256_file(path)
    if actual_size != expected_size or actual_hash != expected:
        raise HostBootstrapError(
            f"integridade divergente: esperado_size={expected_size} atual_size={actual_size} "
            f"esperado_sha256={expected} atual_sha256={actual_hash}"
        )


def download_verified_file(
    url: str,
    target: Path,
    *,
    expected_sha256: str,
    expected_size: int,
    max_bytes: int,
    timeout: int = 60,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "ReqSys-Host-Bootstrap/1"})
    total = 0
    with urllib.request.urlopen(request, timeout=timeout) as response, target.open("wb") as handle:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise HostBootstrapError(f"download excede limite: {url}")
            handle.write(chunk)
    verify_file(target, expected_sha256, expected_size)


def _safe_zip_destination(root: Path, member_name: str) -> Path:
    if not member_name or "\\" in member_name or ":" in member_name:
        raise HostBootstrapError(f"entrada ZIP insegura: {member_name!r}")
    posix = PurePosixPath(member_name)
    if posix.is_absolute() or any(part in ("", ".", "..") for part in posix.parts):
        raise HostBootstrapError(f"entrada ZIP insegura: {member_name!r}")
    root_resolved = root.resolve()
    destination = root.joinpath(*posix.parts).resolve()
    try:
        common = Path(os.path.commonpath([str(root_resolved), str(destination)]))
    except ValueError as exc:
        raise HostBootstrapError(f"entrada ZIP fora do destino: {member_name!r}") from exc
    if common != root_resolved:
        raise HostBootstrapError(f"entrada ZIP fora do destino: {member_name!r}")
    return destination


def safe_extract_zip(archive_path: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = archive.infolist()
            if len(members) > MAX_MINGIT_MEMBERS:
                raise HostBootstrapError("ZIP MinGit excede limite de entradas")
            total_uncompressed = 0
            for info in members:
                total_uncompressed += int(info.file_size)
                if total_uncompressed > MAX_MINGIT_UNCOMPRESSED_BYTES:
                    raise HostBootstrapError("ZIP MinGit excede limite descompactado")
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise HostBootstrapError(f"ZIP MinGit contém symlink: {info.filename}")
                destination = _safe_zip_destination(target, info.filename.rstrip("/"))
                if info.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info, "r") as source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
    except zipfile.BadZipFile as exc:
        raise HostBootstrapError("arquivo MinGit não é ZIP válido") from exc


def parse_manifest(payload: bytes) -> dict[str, Any]:
    try:
        manifest = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HostBootstrapError("MANIFEST.json inválido") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise HostBootstrapError("MANIFEST.json sem estrutura esperada")
    if not isinstance(manifest.get("version"), str) or not manifest["version"].strip():
        raise HostBootstrapError("MANIFEST.json sem versão")
    return manifest


def manifest_index(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for item in manifest["files"]:
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            index[item["path"]] = item
    for source in RUNTIME_MAP:
        if source not in index:
            raise HostBootstrapError(f"manifesto não contém arquivo obrigatório: {source}")
    return index


def verify_payload(rel: str, payload: bytes, entry: dict[str, Any]) -> None:
    expected_hash = str(entry.get("sha256", "")).lower()
    expected_size = entry.get("size")
    if sha256_bytes(payload) != expected_hash or len(payload) != expected_size:
        raise HostBootstrapError(f"integridade divergente: {rel}")


def _manifest_script_modules(index: dict[str, dict[str, Any]]) -> dict[str, str]:
    modules: dict[str, str] = {}
    scripts_root = PurePosixPath("scripts")
    for rel in index:
        path = PurePosixPath(rel)
        if path.parent == scripts_root and path.suffix == ".py":
            modules[path.stem] = rel
    return modules


def validate_runtime_import_closure(
    bundle_dir: Path, index: dict[str, dict[str, Any]]
) -> None:
    """Fail closed when an installed Python entrypoint imports an omitted local module."""
    local_modules = _manifest_script_modules(index)
    mapped_modules = {
        PurePosixPath(source).stem: source
        for source, destination in RUNTIME_MAP.items()
        if PurePosixPath(source).suffix == ".py"
        and PurePosixPath(destination).parent == PurePosixPath("bin")
    }
    missing: list[str] = []
    for source in sorted(mapped_modules.values()):
        path = bundle_dir / source
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=source)
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            raise HostBootstrapError(
                f"não foi possível analisar imports do runtime: {source}: {exc}"
            ) from exc
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".", 1)[0])
        for module in sorted(imported):
            local_source = local_modules.get(module)
            if local_source and module not in mapped_modules:
                missing.append(f"{source} -> {local_source}")
    if missing:
        raise HostBootstrapError(
            "fechamento transitivo incompleto do runtime: " + ", ".join(missing)
        )


def download_bundle(commit: str, bundle_dir: Path) -> dict[str, Any]:
    commit = validate_commit(commit)
    bundle_dir.mkdir(parents=True, exist_ok=True)
    base = f"{RAW_ROOT}/{commit}"
    manifest_payload = download_bytes(f"{base}/MANIFEST.json")
    manifest = parse_manifest(manifest_payload)
    index = manifest_index(manifest)
    (bundle_dir / "MANIFEST.json").write_bytes(manifest_payload)
    for source in RUNTIME_MAP:
        payload = download_bytes(f"{base}/{source}")
        verify_payload(source, payload, index[source])
        target = bundle_dir / source
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    validate_runtime_import_closure(bundle_dir, index)
    return manifest


def verify_bundle(bundle_dir: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    manifest_path = bundle_dir / "MANIFEST.json"
    if not manifest_path.is_file():
        raise HostBootstrapError("bundle sem MANIFEST.json")
    manifest = parse_manifest(manifest_path.read_bytes())
    index = manifest_index(manifest)
    for source in RUNTIME_MAP:
        path = bundle_dir / source
        if not path.is_file():
            raise HostBootstrapError(f"bundle sem arquivo obrigatório: {source}")
        verify_payload(source, path.read_bytes(), index[source])
    validate_runtime_import_closure(bundle_dir, index)
    return manifest, index


_WINDOWS_SECURITY_INFORMATION = 0x00000001 | 0x00000002 | 0x00000004
_WINDOWS_DACL_AUTO_INHERITED = 0x0400


def _windows_copy_file(source: Path, destination: Path) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    copy_file = kernel32.CopyFileExW
    copy_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPVOID,
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.BOOL),
        wintypes.DWORD,
    ]
    copy_file.restype = wintypes.BOOL
    cancelled = wintypes.BOOL(False)
    copy_file_fail_if_exists = 0x00000001
    if not copy_file(
        str(source),
        str(destination),
        None,
        None,
        ctypes.byref(cancelled),
        copy_file_fail_if_exists,
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _windows_move_file(source: Path, destination: Path) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move_file = kernel32.MoveFileExW
    move_file.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    move_file.restype = wintypes.BOOL
    move_file_replace_existing = 0x00000001
    move_file_write_through = 0x00000008
    if not move_file(
        str(source),
        str(destination),
        move_file_replace_existing | move_file_write_through,
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _windows_get_security_descriptor(path: Path) -> bytes:
    import ctypes
    from ctypes import wintypes

    security = ctypes.WinDLL("advapi32", use_last_error=True)
    get_security = security.GetFileSecurityW
    get_security.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    get_security.restype = wintypes.BOOL
    needed = wintypes.DWORD()
    ctypes.set_last_error(0)
    get_security(
        str(path),
        _WINDOWS_SECURITY_INFORMATION,
        None,
        0,
        ctypes.byref(needed),
    )
    error = ctypes.get_last_error()
    if error != 122 or not needed.value:
        raise ctypes.WinError(error)
    buffer = ctypes.create_string_buffer(needed.value)
    if not get_security(
        str(path),
        _WINDOWS_SECURITY_INFORMATION,
        buffer,
        needed.value,
        ctypes.byref(needed),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return bytes(buffer.raw[: needed.value])


def _windows_dacl_security_information(security: dict[str, Any]) -> int:
    protected = security.get("dacl_protected")
    if type(protected) is not bool:
        raise OSError("estado de proteção da DACL Windows inválido")
    dacl_security_information = 0x00000004
    protected_dacl_security_information = 0x80000000
    # DACL_SECURITY_INFORMATION preserves an already-unprotected stage without
    # asking Windows to propagate inheritance again.  Explicitly requesting
    # UNPROTECTED_DACL_SECURITY_INFORMATION can materialize inherited ACEs and
    # therefore change the ACL we are trying to preserve.  A protected source
    # still needs the protection bit applied to the inactive stage.
    return dacl_security_information | (
        protected_dacl_security_information if protected else 0
    )


def _windows_set_dacl(path: Path, descriptor: bytes) -> None:
    import ctypes
    from ctypes import wintypes

    security = ctypes.WinDLL("advapi32", use_last_error=True)
    get_dacl = security.GetSecurityDescriptorDacl
    get_dacl.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.BOOL),
    ]
    get_dacl.restype = wintypes.BOOL
    set_named_security = security.SetNamedSecurityInfoW
    set_named_security.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    set_named_security.restype = wintypes.DWORD
    buffer = ctypes.create_string_buffer(descriptor, len(descriptor))
    descriptor_security = _windows_security_fingerprint(descriptor)
    dacl_present = wintypes.BOOL()
    dacl = wintypes.LPVOID()
    dacl_defaulted = wintypes.BOOL()
    if not get_dacl(
        buffer,
        ctypes.byref(dacl_present),
        ctypes.byref(dacl),
        ctypes.byref(dacl_defaulted),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    if not dacl_present.value:
        raise OSError("security descriptor Windows sem DACL presente")
    error = set_named_security(
        str(path),
        1,  # SE_FILE_OBJECT
        _windows_dacl_security_information(descriptor_security),
        None,
        None,
        dacl,
        None,
    )
    if error:
        raise ctypes.WinError(error)


def _windows_get_file_attributes(path: Path) -> int:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_attributes = kernel32.GetFileAttributesW
    get_attributes.argtypes = [wintypes.LPCWSTR]
    get_attributes.restype = wintypes.DWORD
    attributes = get_attributes(str(path))
    if attributes == 0xFFFFFFFF:
        raise ctypes.WinError(ctypes.get_last_error())
    return int(attributes)


def _windows_set_file_attributes(path: Path, attributes: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_attributes = kernel32.SetFileAttributesW
    set_attributes.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    set_attributes.restype = wintypes.BOOL
    if not set_attributes(str(path), attributes):
        raise ctypes.WinError(ctypes.get_last_error())


def _windows_file_identity(path: Path) -> tuple[int, int]:
    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("last_access_time", wintypes.FILETIME),
            ("last_write_time", wintypes.FILETIME),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

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
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ByHandleFileInformation),
    ]
    get_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    file_read_attributes = 0x00000080
    share_read_write_delete = 0x00000001 | 0x00000002 | 0x00000004
    open_existing = 3
    handle = create_file(
        str(path),
        file_read_attributes,
        share_read_write_delete,
        None,
        open_existing,
        0,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    information = ByHandleFileInformation()
    try:
        if not get_information(handle, ctypes.byref(information)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if not close_handle(handle):
            raise ctypes.WinError(ctypes.get_last_error())
    file_index = (
        int(information.file_index_high) << 32
    ) | int(information.file_index_low)
    return int(information.volume_serial_number), file_index


def _windows_stream_fingerprint(path: Path) -> tuple[tuple[str, int, str], ...]:
    """Inventaria ADS nomeados; o stream default muda por definição."""
    import ctypes
    from ctypes import wintypes

    class FindStreamData(ctypes.Structure):
        _fields_ = [
            ("stream_size", ctypes.c_longlong),
            ("stream_name", wintypes.WCHAR * (260 + 36)),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    find_first = kernel32.FindFirstStreamW
    find_first.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(FindStreamData),
        wintypes.DWORD,
    ]
    find_first.restype = wintypes.HANDLE
    find_next = kernel32.FindNextStreamW
    find_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(FindStreamData)]
    find_next.restype = wintypes.BOOL
    find_close = kernel32.FindClose
    find_close.argtypes = [wintypes.HANDLE]
    find_close.restype = wintypes.BOOL

    data = FindStreamData()
    handle = find_first(str(path), 0, ctypes.byref(data), 0)
    invalid_handle = wintypes.HANDLE(-1).value
    if handle == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    streams: list[tuple[str, int, str]] = []
    try:
        while True:
            name = data.stream_name
            if name != "::$DATA":
                stream_path = Path(str(path) + name)
                payload = stream_path.read_bytes()
                if len(payload) != data.stream_size:
                    raise OSError(f"tamanho do ADS mudou durante inventário: {name}")
                streams.append((name, len(payload), sha256_bytes(payload)))
            if find_next(handle, ctypes.byref(data)):
                continue
            error = ctypes.get_last_error()
            if error != 38:
                raise ctypes.WinError(error)
            break
    finally:
        if not find_close(handle):
            raise ctypes.WinError(ctypes.get_last_error())
    return tuple(sorted(streams))


def _windows_file_state(path: Path) -> dict[str, Any]:
    identity = _windows_file_identity(path)
    content = (path.stat().st_size, sha256_file(path))
    descriptor = _windows_get_security_descriptor(path)
    security = _windows_security_fingerprint(descriptor)
    attributes = _windows_get_file_attributes(path)
    streams = _windows_stream_fingerprint(path)
    final_identity = _windows_file_identity(path)
    final_content = (path.stat().st_size, sha256_file(path))
    final_descriptor = _windows_get_security_descriptor(path)
    final_security = _windows_security_fingerprint(final_descriptor)
    final_attributes = _windows_get_file_attributes(path)
    final_streams = _windows_stream_fingerprint(path)
    if (
        final_identity != identity
        or final_content != content
        or final_attributes != attributes
        or final_streams != streams
        or final_descriptor != descriptor
    ):
        raise OSError(f"arquivo Windows mudou durante o snapshot: {path}")
    return {
        "identity": final_identity,
        "content": final_content,
        "security_descriptor": final_descriptor,
        "security": final_security,
        "attributes": final_attributes,
        "streams": final_streams,
    }


def _windows_security_fingerprint(descriptor: bytes) -> dict[str, Any]:
    """Extrai owner/group/DACL e ACEs sem comparar bytes de bookkeeping."""
    import ctypes
    from ctypes import wintypes

    class Acl(ctypes.Structure):
        _fields_ = [
            ("revision", wintypes.BYTE),
            ("reserved_1", wintypes.BYTE),
            ("size", wintypes.WORD),
            ("ace_count", wintypes.WORD),
            ("reserved_2", wintypes.WORD),
        ]

    class AceHeader(ctypes.Structure):
        _fields_ = [
            ("type", wintypes.BYTE),
            ("flags", wintypes.BYTE),
            ("size", wintypes.WORD),
        ]

    security = ctypes.WinDLL("advapi32", use_last_error=True)
    get_owner = security.GetSecurityDescriptorOwner
    get_owner.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.BOOL),
    ]
    get_owner.restype = wintypes.BOOL
    get_group = security.GetSecurityDescriptorGroup
    get_group.argtypes = get_owner.argtypes
    get_group.restype = wintypes.BOOL
    get_dacl = security.GetSecurityDescriptorDacl
    get_dacl.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.BOOL),
    ]
    get_dacl.restype = wintypes.BOOL
    get_control = security.GetSecurityDescriptorControl
    get_control.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    get_control.restype = wintypes.BOOL
    is_valid_sid = security.IsValidSid
    is_valid_sid.argtypes = [wintypes.LPVOID]
    is_valid_sid.restype = wintypes.BOOL
    get_length_sid = security.GetLengthSid
    get_length_sid.argtypes = [wintypes.LPVOID]
    get_length_sid.restype = wintypes.DWORD
    get_ace = security.GetAce
    get_ace.argtypes = [
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
    ]
    get_ace.restype = wintypes.BOOL

    buffer = ctypes.create_string_buffer(descriptor, len(descriptor))

    def checked(result: int, operation: str) -> None:
        if not result:
            raise OSError(
                ctypes.get_last_error(),
                f"Windows security descriptor inválido durante {operation}",
            )

    def sid_bytes(pointer: wintypes.LPVOID) -> bytes | None:
        if not pointer.value:
            return None
        checked(is_valid_sid(pointer), "IsValidSid")
        length = get_length_sid(pointer)
        if not length:
            raise OSError("Windows security descriptor contém SID vazio")
        return ctypes.string_at(pointer, length)

    def ace_state(pointer: wintypes.LPVOID) -> tuple[Any, ...]:
        header = AceHeader.from_address(pointer.value)
        if header.size < 8:
            raise OSError("Windows security descriptor contém ACE truncada")
        raw = ctypes.string_at(pointer, header.size)
        basic_sid_types = {
            0x00, 0x01, 0x02, 0x03, 0x09, 0x0A, 0x0D, 0x0E,
            0x11, 0x12, 0x13, 0x14, 0x15,
        }
        object_sid_types = {
            0x05, 0x06, 0x07, 0x08, 0x0B, 0x0C, 0x0F, 0x10,
        }
        if header.type in basic_sid_types:
            sid_offset = 8
        elif header.type in object_sid_types:
            if header.size < 12:
                raise OSError("Windows security descriptor contém object ACE truncada")
            object_flags = int.from_bytes(raw[8:12], "little")
            if object_flags & ~0x03:
                raise OSError("Windows security descriptor contém object ACE inválida")
            sid_offset = 12 + (16 if object_flags & 0x01 else 0)
            sid_offset += 16 if object_flags & 0x02 else 0
        elif header.type == 0x04:
            sid_offset = 12
        else:
            raise OSError(
                f"tipo de ACE não suportado para atualização segura: {header.type}"
            )
        if sid_offset >= header.size:
            raise OSError("Windows security descriptor contém ACE sem SID")
        sid = sid_bytes(wintypes.LPVOID(pointer.value + sid_offset))
        if sid is None or sid_offset + len(sid) > header.size:
            raise OSError("Windows security descriptor contém SID fora da ACE")
        sid_end = sid_offset + len(sid)
        return (
            header.type,
            header.flags,
            header.size,
            int.from_bytes(raw[4:8], "little"),
            raw[8:sid_offset],
            sid,
            raw[sid_end:],
        )

    owner = wintypes.LPVOID()
    owner_defaulted = wintypes.BOOL()
    group = wintypes.LPVOID()
    group_defaulted = wintypes.BOOL()
    dacl_present = wintypes.BOOL()
    dacl = wintypes.LPVOID()
    dacl_defaulted = wintypes.BOOL()
    control = wintypes.WORD()
    revision = wintypes.DWORD()
    checked(
        get_owner(buffer, ctypes.byref(owner), ctypes.byref(owner_defaulted)),
        "GetSecurityDescriptorOwner",
    )
    checked(
        get_group(buffer, ctypes.byref(group), ctypes.byref(group_defaulted)),
        "GetSecurityDescriptorGroup",
    )
    checked(
        get_dacl(
            buffer,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl),
            ctypes.byref(dacl_defaulted),
        ),
        "GetSecurityDescriptorDacl",
    )
    checked(
        get_control(buffer, ctypes.byref(control), ctypes.byref(revision)),
        "GetSecurityDescriptorControl",
    )
    acl_state = None
    if dacl.value:
        header = Acl.from_address(dacl.value)
        aces = []
        for index in range(header.ace_count):
            ace = wintypes.LPVOID()
            checked(get_ace(dacl, index, ctypes.byref(ace)), "GetAce")
            aces.append(ace_state(ace))
        acl_state = (header.revision, header.ace_count, tuple(aces))
    return {
        "owner_sid": sid_bytes(owner),
        "owner_defaulted": bool(owner_defaulted.value),
        "group_sid": sid_bytes(group),
        "group_defaulted": bool(group_defaulted.value),
        "dacl_present": bool(dacl_present.value),
        "dacl_null": bool(dacl_present.value and not dacl.value),
        "dacl_defaulted": bool(dacl_defaulted.value),
        "dacl_protected": bool(control.value & 0x1000),
        "dacl": acl_state,
        "control": control.value,
        "revision": revision.value,
    }


def _windows_security_diagnostic(security: dict[str, Any]) -> dict[str, Any]:
    """Summarize an ACL mismatch without publishing local account SIDs."""

    def digest(value: bytes | None) -> str | None:
        return None if value is None else sha256_bytes(value)

    dacl = security["dacl"]
    summarized_dacl = None
    if dacl is not None:
        revision, ace_count, aces = dacl
        summarized_dacl = {
            "revision": revision,
            "ace_count": ace_count,
            "aces": tuple(
                {
                    "type": ace[0],
                    "flags": ace[1],
                    "size": ace[2],
                    "mask": ace[3],
                    "opaque_before_sid_sha256": digest(ace[4]),
                    "sid_sha256": digest(ace[5]),
                    "opaque_tail_sha256": digest(ace[6]),
                }
                for ace in aces
            ),
        }
    return {
        "owner_sid_sha256": digest(security["owner_sid"]),
        "owner_defaulted": security["owner_defaulted"],
        "group_sid_sha256": digest(security["group_sid"]),
        "group_defaulted": security["group_defaulted"],
        "dacl_present": security["dacl_present"],
        "dacl_null": security["dacl_null"],
        "dacl_defaulted": security["dacl_defaulted"],
        "dacl_protected": security["dacl_protected"],
        "dacl": summarized_dacl,
        "control": f"0x{int(security['control']):04x}",
        "revision": security["revision"],
    }


def _windows_security_equivalent(
    expected: dict[str, Any], actual: dict[str, Any]
) -> bool:
    semantic_fields = (
        "owner_sid",
        "owner_defaulted",
        "group_sid",
        "group_defaulted",
        "dacl_present",
        "dacl_null",
        "dacl_defaulted",
        "dacl_protected",
        "dacl",
        "revision",
    )
    if any(expected[field] != actual[field] for field in semantic_fields):
        return False
    expected_control = int(expected["control"])
    actual_control = int(actual["control"])
    if (
        expected_control & ~_WINDOWS_DACL_AUTO_INHERITED
        != actual_control & ~_WINDOWS_DACL_AUTO_INHERITED
    ):
        return False
    changed = expected_control ^ actual_control
    return changed == 0 or (
        changed == _WINDOWS_DACL_AUTO_INHERITED
        and not expected_control & _WINDOWS_DACL_AUTO_INHERITED
        and bool(actual_control & _WINDOWS_DACL_AUTO_INHERITED)
    )


def _windows_owner_group_equivalent(
    expected: dict[str, Any], actual: dict[str, Any]
) -> bool:
    return all(
        expected[field] == actual[field]
        for field in (
            "owner_sid",
            "owner_defaulted",
            "group_sid",
            "group_defaulted",
        )
    )


def _windows_metadata_required() -> bool:
    return os.name == "nt"


def _windows_persisted_security(security: dict[str, Any]) -> dict[str, Any]:
    dacl = security["dacl"]
    persisted_dacl = None
    if dacl is not None:
        revision, ace_count, aces = dacl
        persisted_dacl = [
            int(revision),
            int(ace_count),
            [
                [
                    int(ace_type),
                    int(flags),
                    int(size),
                    int(mask),
                    prefix.hex(),
                    sid.hex(),
                    suffix.hex(),
                ]
                for ace_type, flags, size, mask, prefix, sid, suffix in aces
            ],
        ]
    return {
        "owner_sid": (
            security["owner_sid"].hex()
            if security["owner_sid"] is not None
            else None
        ),
        "owner_defaulted": bool(security["owner_defaulted"]),
        "group_sid": (
            security["group_sid"].hex()
            if security["group_sid"] is not None
            else None
        ),
        "group_defaulted": bool(security["group_defaulted"]),
        "dacl_present": bool(security["dacl_present"]),
        "dacl_null": bool(security["dacl_null"]),
        "dacl_defaulted": bool(security["dacl_defaulted"]),
        "dacl_protected": bool(security["dacl_protected"]),
        "dacl": persisted_dacl,
        "control": int(security["control"]),
        "revision": int(security["revision"]),
    }


def _windows_persisted_file_state(path: Path) -> dict[str, Any]:
    state = _windows_file_state(path)
    return {
        "version": 1,
        "attributes": int(state["attributes"]),
        "security": _windows_persisted_security(state["security"]),
        "streams": [
            {"name": name, "size": size, "sha256": digest}
            for name, size, digest in state["streams"]
        ],
    }


def _validate_hex_bytes(
    value: Any, *, field: str, maximum_bytes: int, allow_none: bool = False
) -> None:
    if value is None and allow_none:
        return
    if not isinstance(value, str) or len(value) % 2 or len(value) > maximum_bytes * 2:
        raise HostBootstrapError(f"campo hexadecimal Windows inválido: {field}")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise HostBootstrapError(
            f"campo hexadecimal Windows inválido: {field}"
        ) from exc


def _validate_windows_persisted_state(state: Any, destination: str) -> None:
    if not isinstance(state, dict) or set(state) != {
        "version",
        "attributes",
        "security",
        "streams",
    }:
        raise HostBootstrapError(
            f"estado Windows inválido no rollback: {destination}"
        )
    if state["version"] != 1:
        raise HostBootstrapError(
            f"versão do estado Windows inválida: {destination}"
        )
    attributes = state["attributes"]
    if type(attributes) is not int or not 0 <= attributes <= 0xFFFFFFFF:
        raise HostBootstrapError(
            f"atributos Windows inválidos no rollback: {destination}"
        )
    security = state["security"]
    security_fields = {
        "owner_sid",
        "owner_defaulted",
        "group_sid",
        "group_defaulted",
        "dacl_present",
        "dacl_null",
        "dacl_defaulted",
        "dacl_protected",
        "dacl",
        "control",
        "revision",
    }
    if not isinstance(security, dict) or set(security) != security_fields:
        raise HostBootstrapError(
            f"segurança Windows inválida no rollback: {destination}"
        )
    _validate_hex_bytes(
        security["owner_sid"],
        field=f"{destination}.owner_sid",
        maximum_bytes=256,
        allow_none=True,
    )
    _validate_hex_bytes(
        security["group_sid"],
        field=f"{destination}.group_sid",
        maximum_bytes=256,
        allow_none=True,
    )
    for field in (
        "owner_defaulted",
        "group_defaulted",
        "dacl_present",
        "dacl_null",
        "dacl_defaulted",
        "dacl_protected",
    ):
        if type(security[field]) is not bool:
            raise HostBootstrapError(
                f"flag de segurança Windows inválida: {destination}.{field}"
            )
    for field, maximum in (("control", 0xFFFF), ("revision", 0xFFFFFFFF)):
        value = security[field]
        if type(value) is not int or not 0 <= value <= maximum:
            raise HostBootstrapError(
                f"campo de segurança Windows inválido: {destination}.{field}"
            )
    dacl = security["dacl"]
    if dacl is not None:
        if (
            not isinstance(dacl, list)
            or len(dacl) != 3
            or type(dacl[0]) is not int
            or type(dacl[1]) is not int
            or not isinstance(dacl[2], list)
            or dacl[1] != len(dacl[2])
            or len(dacl[2]) > MAX_WINDOWS_ROLLBACK_ACES
        ):
            raise HostBootstrapError(
                f"DACL Windows inválida no rollback: {destination}"
            )
        for index, ace in enumerate(dacl[2]):
            if (
                not isinstance(ace, list)
                or len(ace) != 7
                or any(type(ace[position]) is not int for position in range(4))
                or not 0 <= ace[0] <= 0xFF
                or not 0 <= ace[1] <= 0xFF
                or not 8 <= ace[2] <= 0xFFFF
                or not 0 <= ace[3] <= 0xFFFFFFFF
            ):
                raise HostBootstrapError(
                    f"ACE Windows inválida no rollback: {destination}[{index}]"
                )
            for position, name in ((4, "prefix"), (5, "sid"), (6, "suffix")):
                _validate_hex_bytes(
                    ace[position],
                    field=f"{destination}.ace[{index}].{name}",
                    maximum_bytes=0xFFFF,
                )
            encoded_size = 8 + sum(len(ace[position]) // 2 for position in (4, 5, 6))
            if encoded_size != ace[2]:
                raise HostBootstrapError(
                    f"tamanho de ACE Windows divergente: {destination}[{index}]"
                )
    streams = state["streams"]
    if not isinstance(streams, list) or len(streams) > MAX_WINDOWS_ROLLBACK_STREAMS:
        raise HostBootstrapError(
            f"inventário de ADS inválido no rollback: {destination}"
        )
    names: list[str] = []
    for stream in streams:
        if not isinstance(stream, dict) or set(stream) != {"name", "size", "sha256"}:
            raise HostBootstrapError(
                f"entrada de ADS inválida no rollback: {destination}"
            )
        name = stream["name"]
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 296
            or not name.startswith(":")
            or not name.endswith(":$DATA")
            or name == "::$DATA"
        ):
            raise HostBootstrapError(
                f"nome de ADS inválido no rollback: {destination}"
            )
        if type(stream["size"]) is not int or stream["size"] < 0:
            raise HostBootstrapError(
                f"tamanho de ADS inválido no rollback: {destination}"
            )
        validate_sha256(stream["sha256"])
        names.append(name)
    if names != sorted(names) or len({name.casefold() for name in names}) != len(names):
        raise HostBootstrapError(
            f"inventário de ADS não canônico no rollback: {destination}"
        )


def _windows_persisted_security_equivalent(
    expected: dict[str, Any], actual: dict[str, Any]
) -> bool:
    semantic_fields = (
        "owner_sid",
        "owner_defaulted",
        "group_sid",
        "group_defaulted",
        "dacl_present",
        "dacl_null",
        "dacl_defaulted",
        "dacl_protected",
        "dacl",
        "revision",
    )
    if any(expected[field] != actual[field] for field in semantic_fields):
        return False
    expected_control = int(expected["control"])
    actual_control = int(actual["control"])
    if (
        expected_control & ~_WINDOWS_DACL_AUTO_INHERITED
        != actual_control & ~_WINDOWS_DACL_AUTO_INHERITED
    ):
        return False
    changed = expected_control ^ actual_control
    return changed == 0 or (
        changed == _WINDOWS_DACL_AUTO_INHERITED
        and not expected_control & _WINDOWS_DACL_AUTO_INHERITED
        and bool(actual_control & _WINDOWS_DACL_AUTO_INHERITED)
    )


def _windows_persisted_state_equivalent(
    expected: dict[str, Any], actual: dict[str, Any]
) -> bool:
    return (
        expected["version"] == actual["version"]
        and expected["attributes"] == actual["attributes"]
        and expected["streams"] == actual["streams"]
        and _windows_persisted_security_equivalent(
            expected["security"], actual["security"]
        )
    )


def _windows_path_matches_persisted_state(
    path: Path, expected: dict[str, Any]
) -> bool:
    try:
        actual = _windows_persisted_file_state(path)
    except OSError:
        return False
    return _windows_persisted_state_equivalent(expected, actual)


def _windows_clone_verified(
    source: Path,
    destination: Path,
    *,
    expected_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_state = _windows_file_state(source)
    source_metadata = {
        "version": 1,
        "attributes": int(source_state["attributes"]),
        "security": _windows_persisted_security(source_state["security"]),
        "streams": [
            {"name": name, "size": size, "sha256": digest}
            for name, size, digest in source_state["streams"]
        ],
    }
    if expected_metadata is not None and not _windows_persisted_state_equivalent(
        expected_metadata, source_metadata
    ):
        raise OSError("origem Windows divergiu do estado persistido")
    if destination.exists() or destination.is_symlink():
        raise OSError(f"clone Windows já existe: {destination}")
    owned = False
    try:
        _windows_copy_file(source, destination)
        owned = True
        clone_state = _windows_file_state(destination)
        if not _windows_owner_group_equivalent(
            source_state["security"], clone_state["security"]
        ):
            raise OSError("clone Windows alterou owner ou group da origem")
        if not _windows_security_equivalent(
            source_state["security"], clone_state["security"]
        ):
            _windows_set_dacl(destination, source_state["security_descriptor"])
        if _windows_get_file_attributes(destination) != source_state["attributes"]:
            _windows_set_file_attributes(destination, source_state["attributes"])
        final_state = _windows_file_state(destination)
        final_metadata = {
            "version": 1,
            "attributes": int(final_state["attributes"]),
            "security": _windows_persisted_security(final_state["security"]),
            "streams": [
                {"name": name, "size": size, "sha256": digest}
                for name, size, digest in final_state["streams"]
            ],
        }
        if (
            final_state["content"] != source_state["content"]
            or not _windows_persisted_state_equivalent(
                source_metadata, final_metadata
            )
        ):
            raise OSError("clone Windows divergiu da origem")
        live_source = _windows_file_state(source)
        live_metadata = {
            "version": 1,
            "attributes": int(live_source["attributes"]),
            "security": _windows_persisted_security(live_source["security"]),
            "streams": [
                {"name": name, "size": size, "sha256": digest}
                for name, size, digest in live_source["streams"]
            ],
        }
        if (
            live_source["identity"] != source_state["identity"]
            or live_source["content"] != source_state["content"]
            or not _windows_persisted_state_equivalent(
                source_metadata, live_metadata
            )
        ):
            raise OSError("origem Windows mudou durante a clonagem")
        return source_metadata
    except Exception:
        if owned:
            _cleanup_owned_stage(destination)
        raise


def _overwrite_default_stream(path: Path, payload: bytes) -> None:
    with path.open("r+b") as handle:
        handle.seek(0)
        handle.truncate(0)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _cleanup_owned_stage(path: Path) -> None:
    if not path.exists():
        return
    if path.is_symlink() or not path.is_file():
        raise OSError(f"stage Windows owned mudou de tipo: {path}")
    path.unlink()


def _windows_replace_file(source: Path, destination: Path) -> None:
    stage = destination.with_name(
        destination.name + f".replace-stage-{uuid.uuid4().hex}"
    )
    stage_owned = False
    primary_error: BaseException | None = None
    operation = "read_source"
    try:
        payload = source.read_bytes()
        operation = "snapshot_destination"
        expected_state = _windows_file_state(destination)
        operation = "validate_stage_absent"
        if stage.exists():
            raise OSError(f"stage Windows já existe: {stage}")
        descriptor = expected_state["security_descriptor"]
        expected_security = expected_state["security"]
        expected_attributes = expected_state["attributes"]
        expected_streams = expected_state["streams"]
        operation = "clone_destination"
        _windows_copy_file(destination, stage)
        stage_owned = True
        operation = "read_clone_security"
        cloned_security = _windows_security_fingerprint(
            _windows_get_security_descriptor(stage)
        )
        if not _windows_owner_group_equivalent(
            expected_security, cloned_security
        ):
            raise OSError("clone Windows alterou owner ou group do destino")
        operation = "read_clone_attributes"
        if _windows_get_file_attributes(stage) != expected_attributes:
            raise OSError("clone Windows alterou atributos do destino")
        operation = "read_clone_streams"
        if _windows_stream_fingerprint(stage) != expected_streams:
            raise OSError("clone Windows alterou ADS do destino")
        operation = "overwrite_default_stream"
        _overwrite_default_stream(stage, payload)
        if not _windows_security_equivalent(expected_security, cloned_security):
            operation = "restore_dacl"
            _windows_set_dacl(stage, descriptor)
        operation = "restore_attributes"
        if _windows_get_file_attributes(stage) != expected_attributes:
            _windows_set_file_attributes(stage, expected_attributes)
        operation = "validate_stage_security"
        actual_security = _windows_security_fingerprint(
            _windows_get_security_descriptor(stage)
        )
        if not _windows_security_equivalent(expected_security, actual_security):
            raise OSError(
                "stage Windows alterou owner, group, DACL ou ACEs do destino; "
                f"esperado={_windows_security_diagnostic(expected_security)!r}; "
                f"clone={_windows_security_diagnostic(cloned_security)!r}; "
                f"atual={_windows_security_diagnostic(actual_security)!r}"
            )
        operation = "validate_stage_content"
        if stage.read_bytes() != payload:
            raise OSError("stage Windows não reteve o payload esperado")
        operation = "validate_stage_attributes"
        if _windows_get_file_attributes(stage) != expected_attributes:
            raise OSError("stage Windows não reteve atributos do destino")
        operation = "validate_stage_streams"
        if _windows_stream_fingerprint(stage) != expected_streams:
            raise OSError("stage Windows não reteve ADS do destino")
        operation = "validate_live_destination"
        live_state = _windows_file_state(destination)
        if (
            live_state["identity"] != expected_state["identity"]
            or live_state["content"] != expected_state["content"]
            or live_state["attributes"] != expected_attributes
            or live_state["streams"] != expected_streams
            or not _windows_security_equivalent(
                expected_security, live_state["security"]
            )
        ):
            raise OSError(
                "destino Windows mudou desde o snapshot; ativação recusada"
            )
        staged_identity = _windows_file_identity(stage)
        operation = "unlink_source"
        source.unlink()
        operation = "activate_stage"
        _windows_move_file(stage, destination)
        stage_owned = False
        operation = "validate_final_destination"
        final_state = _windows_file_state(destination)
        if (
            final_state["identity"] != staged_identity
            or final_state["content"] != (len(payload), sha256_bytes(payload))
            or final_state["attributes"] != expected_attributes
            or final_state["streams"] != expected_streams
            or not _windows_security_equivalent(
                expected_security, final_state["security"]
            )
        ):
            raise OSError("readback final do destino Windows divergiu do stage")
    except Exception as exc:
        primary_error = exc
        if isinstance(exc, WindowsReplaceError):
            raise
        raise WindowsReplaceError(operation, exc) from exc
    finally:
        if stage_owned:
            try:
                _cleanup_owned_stage(stage)
            except OSError as cleanup_error:
                if primary_error is None:
                    raise WindowsReplaceError(
                        "cleanup_stage", cleanup_error
                    ) from cleanup_error
                raise WindowsReplaceError(
                    "cleanup_stage", cleanup_error
                ) from primary_error


def replace_file(source: Path, destination: Path) -> None:
    """Substitui uma folha sem expor metadados Windows parcialmente aplicados."""
    if os.name == "nt" and destination.exists():
        _windows_replace_file(source, destination)
        return
    os.replace(source, destination)


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".tmp-{uuid.uuid4().hex}")
    temp.write_bytes(payload)
    try:
        replace_file(temp, path)
    except OSError:
        try:
            if (
                temp.is_file()
                and not temp.is_symlink()
                and temp.read_bytes() == payload
            ):
                temp.unlink()
        except OSError:
            pass
        raise


def _managed_target(install_root: Path, destination: str) -> Path:
    relative = PurePosixPath(destination)
    if relative.is_absolute() or not relative.parts or any(
        part in ("", ".", "..") for part in relative.parts
    ):
        raise HostBootstrapError(f"destino gerenciado inválido: {destination}")
    root = install_root.resolve()
    target = root.joinpath(*relative.parts)
    try:
        common = Path(os.path.commonpath([str(root), str(target.resolve())]))
    except ValueError as exc:
        raise HostBootstrapError(
            f"destino gerenciado fora da instalação: {destination}"
        ) from exc
    if common != root:
        raise HostBootstrapError(
            f"destino gerenciado fora da instalação: {destination}"
        )
    return target


def prepare_runtime_rollback(
    bundle_dir: Path, install_root: Path
) -> Path:
    """Persist a verified rollback snapshot before the first runtime mutation."""
    _, index = verify_bundle(bundle_dir)
    install_root.mkdir(parents=True, exist_ok=True)
    backup_dir = install_root / (
        f"backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex}"
    )
    windows_metadata = _windows_metadata_required()
    entries: dict[str, dict[str, Any]] = {}
    for source, destination in RUNTIME_MAP.items():
        target = _managed_target(install_root, destination)
        desired = (bundle_dir / source).read_bytes()
        expected = index[source]
        entry: dict[str, Any] = {
            "source": source,
            "existed": target.exists(),
            "desired_sha256": sha256_bytes(desired),
            "desired_size": len(desired),
        }
        if (
            entry["desired_sha256"] != str(expected.get("sha256", "")).lower()
            or entry["desired_size"] != expected.get("size")
        ):
            raise HostBootstrapError(
                f"payload divergiu antes do snapshot: {source}"
            )
        if target.exists():
            if target.is_symlink() or not target.is_file():
                raise HostBootstrapError(
                    f"arquivo gerenciado preexistente inválido: {destination}"
                )
            previous = backup_dir / "files" / destination
            previous.parent.mkdir(parents=True, exist_ok=True)
            if windows_metadata:
                entry["previous_windows_state"] = _windows_clone_verified(
                    target, previous
                )
            else:
                shutil.copy2(target, previous)
            current_stat = target.stat()
            entry.update(
                {
                    "previous_sha256": sha256_file(target),
                    "previous_size": current_stat.st_size,
                    "previous_mode": stat.S_IMODE(current_stat.st_mode),
                }
            )
        entries[destination] = entry
    snapshot = {
        "version": ROLLBACK_MANIFEST_VERSION,
        "metadata_contract": (
            WINDOWS_ROLLBACK_METADATA_CONTRACT
            if windows_metadata
            else POSIX_ROLLBACK_METADATA_CONTRACT
        ),
        "install_root": str(install_root.resolve()),
        "created_at": utc_now(),
        "entries": entries,
    }
    atomic_write(
        backup_dir / "rollback-manifest.json",
        (
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n"
        ).encode("utf-8"),
    )
    return backup_dir


def _load_runtime_rollback(
    install_root: Path, backup_dir: Path
) -> dict[str, Any]:
    root = install_root.resolve()
    backup = backup_dir.resolve()
    try:
        common = Path(os.path.commonpath([str(root), str(backup)]))
    except ValueError as exc:
        raise HostBootstrapError("backup fora da instalação") from exc
    if common != root:
        raise HostBootstrapError("backup fora da instalação")
    manifest_path = backup / "rollback-manifest.json"
    try:
        snapshot = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HostBootstrapError("manifesto de rollback ausente ou inválido") from exc
    version = snapshot.get("version")
    windows_metadata = _windows_metadata_required()
    if version == 1:
        if windows_metadata:
            raise HostBootstrapError(
                "manifesto de rollback v1 não comprova ACL/ADS no Windows"
            )
    elif version == ROLLBACK_MANIFEST_VERSION:
        expected_contract = (
            WINDOWS_ROLLBACK_METADATA_CONTRACT
            if windows_metadata
            else POSIX_ROLLBACK_METADATA_CONTRACT
        )
        if snapshot.get("metadata_contract") != expected_contract:
            raise HostBootstrapError(
                "contrato de metadados do rollback divergente"
            )
    else:
        raise HostBootstrapError("versão do manifesto de rollback inválida")
    if os.path.normcase(str(Path(snapshot.get("install_root", "")).resolve())) != os.path.normcase(str(root)):
        raise HostBootstrapError("raiz do manifesto de rollback divergente")
    entries = snapshot.get("entries")
    if not isinstance(entries, dict) or set(entries) != set(RUNTIME_MAP.values()):
        raise HostBootstrapError("conjunto gerenciado do rollback divergente")
    for destination, entry in entries.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("existed"), bool):
            raise HostBootstrapError(
                f"entrada de rollback inválida: {destination}"
            )
        validate_sha256(str(entry.get("desired_sha256", "")))
        if not isinstance(entry.get("desired_size"), int):
            raise HostBootstrapError(
                f"tamanho desejado inválido no rollback: {destination}"
            )
        if entry["existed"]:
            previous = backup / "files" / destination
            if previous.is_symlink() or not previous.is_file():
                raise HostBootstrapError(
                    f"backup gerenciado ausente: {destination}"
                )
            if (
                sha256_file(previous) != entry.get("previous_sha256")
                or previous.stat().st_size != entry.get("previous_size")
                or stat.S_IMODE(previous.stat().st_mode)
                != entry.get("previous_mode")
            ):
                raise HostBootstrapError(
                    f"backup gerenciado divergente: {destination}"
                )
            if windows_metadata:
                previous_windows_state = entry.get("previous_windows_state")
                _validate_windows_persisted_state(
                    previous_windows_state, destination
                )
                try:
                    observed_windows_state = _windows_persisted_file_state(previous)
                except OSError as exc:
                    raise HostBootstrapError(
                        f"backup Windows ilegível: {destination}"
                    ) from exc
                if not _windows_persisted_state_equivalent(
                    previous_windows_state, observed_windows_state
                ):
                    raise HostBootstrapError(
                        f"metadados do backup Windows divergentes: {destination}"
                    )
        elif windows_metadata and "previous_windows_state" in entry:
            raise HostBootstrapError(
                f"estado Windows inesperado para arquivo novo: {destination}"
            )
    return snapshot


def validate_runtime_rollback_for_bundle(
    bundle_dir: Path, install_root: Path, backup_dir: Path
) -> None:
    _, index = verify_bundle(bundle_dir)
    snapshot = _load_runtime_rollback(install_root, backup_dir)
    by_destination = snapshot["entries"]
    for source, destination in RUNTIME_MAP.items():
        entry = by_destination[destination]
        if (
            entry["source"] != source
            or entry["desired_sha256"]
            != str(index[source].get("sha256", "")).lower()
            or entry["desired_size"] != index[source].get("size")
        ):
            raise HostBootstrapError(
                f"snapshot não pertence ao bundle: {destination}"
            )


def restore_runtime_rollback(
    install_root: Path, backup_dir: Path
) -> None:
    """Restore exactly the prior managed state without touching unmanaged files."""
    snapshot = _load_runtime_rollback(install_root, backup_dir)
    backup = backup_dir.resolve()
    windows_metadata = _windows_metadata_required()
    for destination, entry in snapshot["entries"].items():
        target = _managed_target(install_root, destination)
        if entry["existed"]:
            if target.is_symlink() or not target.is_file():
                raise HostBootstrapError(
                    f"arquivo existente divergente não será sobrescrito: {destination}"
                )
            observed = target.stat()
            current_sha256 = sha256_file(target)
            metadata_matches = (
                _windows_path_matches_persisted_state(
                    target, entry["previous_windows_state"]
                )
                if windows_metadata
                else True
            )
            if (
                current_sha256 == entry["previous_sha256"]
                and observed.st_size == entry["previous_size"]
                and stat.S_IMODE(observed.st_mode) == entry["previous_mode"]
                and metadata_matches
            ):
                continue
            if (
                current_sha256 != entry["desired_sha256"]
                or observed.st_size != entry["desired_size"]
                or not metadata_matches
            ):
                raise HostBootstrapError(
                    f"arquivo existente divergente não será sobrescrito: {destination}"
                )
            previous = backup / "files" / destination
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(
                target.name + f".rollback-{uuid.uuid4().hex}"
            )
            if windows_metadata:
                _windows_clone_verified(
                    previous,
                    temporary,
                    expected_metadata=entry["previous_windows_state"],
                )
                temporary_owned = True
                try:
                    live_observed = target.stat()
                    if (
                        sha256_file(target) != entry["desired_sha256"]
                        or live_observed.st_size != entry["desired_size"]
                        or not _windows_path_matches_persisted_state(
                            target, entry["previous_windows_state"]
                        )
                    ):
                        raise HostBootstrapError(
                            "destino Windows mudou antes da ativação do rollback: "
                            f"{destination}"
                        )
                    staged_identity = _windows_file_identity(temporary)
                    _windows_move_file(temporary, target)
                    temporary_owned = False
                    if _windows_file_identity(target) != staged_identity:
                        raise HostBootstrapError(
                            "rollback Windows não ativou o clone verificado: "
                            f"{destination}"
                        )
                finally:
                    if temporary_owned:
                        _cleanup_owned_stage(temporary)
            else:
                shutil.copy2(previous, temporary)
                try:
                    replace_file(temporary, target)
                except OSError:
                    try:
                        if (
                            temporary.is_file()
                            and not temporary.is_symlink()
                            and temporary.stat().st_size == entry["previous_size"]
                            and sha256_file(temporary) == entry["previous_sha256"]
                        ):
                            try:
                                temporary.unlink()
                            except PermissionError:
                                temporary.chmod(stat.S_IREAD | stat.S_IWRITE)
                                temporary.unlink()
                    except OSError:
                        pass
                    raise
        elif target.exists():
            if target.is_symlink() or not target.is_file():
                raise HostBootstrapError(
                    f"arquivo novo gerenciado não é removível com segurança: {destination}"
                )
            if (
                sha256_file(target) != entry["desired_sha256"]
                or target.stat().st_size != entry["desired_size"]
            ):
                raise HostBootstrapError(
                    f"arquivo novo divergente não será removido: {destination}"
                )
            if windows_metadata:
                try:
                    streams = _windows_persisted_file_state(target)["streams"]
                except OSError as exc:
                    raise HostBootstrapError(
                        f"ADS do arquivo novo ilegível: {destination}"
                    ) from exc
                if streams:
                    raise HostBootstrapError(
                        f"arquivo novo com ADS não será removido: {destination}"
                    )
            target.unlink()

    for destination, entry in snapshot["entries"].items():
        target = _managed_target(install_root, destination)
        if entry["existed"]:
            if target.is_symlink() or not target.is_file():
                raise HostBootstrapError(
                    f"rollback não restaurou arquivo: {destination}"
                )
            observed = target.stat()
            if (
                sha256_file(target) != entry["previous_sha256"]
                or observed.st_size != entry["previous_size"]
                or stat.S_IMODE(observed.st_mode) != entry["previous_mode"]
                or (
                    windows_metadata
                    and not _windows_path_matches_persisted_state(
                        target, entry["previous_windows_state"]
                    )
                )
            ):
                raise HostBootstrapError(
                    f"rollback pós-condição divergente: {destination}"
                )
        elif target.exists():
            raise HostBootstrapError(
                f"rollback não removeu arquivo novo: {destination}"
            )


def install_bundle(
    bundle_dir: Path,
    install_root: Path,
    work_root: Path,
    source_commit: str,
    *,
    rollback_dir: Path | None = None,
    rollback_on_error: bool = True,
    write_receipt: bool = True,
) -> dict[str, Any]:
    source_commit = validate_commit(source_commit)
    manifest, index = verify_bundle(bundle_dir)
    backup_dir = rollback_dir or prepare_runtime_rollback(
        bundle_dir, install_root
    )
    validate_runtime_rollback_for_bundle(
        bundle_dir, install_root, backup_dir
    )
    rollback_snapshot = _load_runtime_rollback(install_root, backup_dir)
    windows_metadata = _windows_metadata_required()
    try:
        installed: dict[str, dict[str, Any]] = {}
        for source, destination in RUNTIME_MAP.items():
            payload = (bundle_dir / source).read_bytes()
            target = _managed_target(install_root, destination)
            try:
                atomic_write(target, payload)
            except WindowsReplaceError as exc:
                raise RuntimeInstallError(
                    destination, exc.operation, exc
                ) from exc
            except OSError as exc:
                raise RuntimeInstallError(
                    destination, "atomic_write", exc
                ) from exc
            installed_file: dict[str, Any] = {
                "sha256": sha256_file(target),
                "size": target.stat().st_size,
            }
            if windows_metadata:
                windows_state = _windows_persisted_file_state(target)
                rollback_entry = rollback_snapshot["entries"][destination]
                if rollback_entry["existed"]:
                    if not _windows_persisted_state_equivalent(
                        rollback_entry["previous_windows_state"], windows_state
                    ):
                        raise HostBootstrapError(
                            "metadados Windows divergiram após instalação: "
                            f"{destination}"
                        )
                elif windows_state["streams"]:
                    raise HostBootstrapError(
                        f"arquivo Windows novo recebeu ADS inesperado: {destination}"
                    )
                installed_file["windows_state"] = windows_state
            installed[destination] = installed_file
        work_root.mkdir(parents=True, exist_ok=True)
        for source, destination in RUNTIME_MAP.items():
            expected = index[source]
            actual = installed[destination]
            if (
                actual["sha256"]
                != str(expected.get("sha256", "")).lower()
                or actual["size"] != expected.get("size")
                or (
                    windows_metadata
                    and not _windows_persisted_state_equivalent(
                        actual["windows_state"],
                        _windows_persisted_file_state(
                            _managed_target(install_root, destination)
                        ),
                    )
                )
            ):
                raise HostBootstrapError(
                    f"verificação pós-instalação falhou: {destination}"
                )
        receipt = {
            "result": "HOST_BOOTSTRAP_RUNTIME_INSTALLED",
            "repository": REPOSITORY,
            "source_commit": source_commit,
            "rules_version": manifest["version"],
            "installed_at": utc_now(),
            "host": socket.gethostname(),
            "install_root": str(install_root),
            "work_root": str(work_root),
            "backup_dir": str(backup_dir),
            "files": installed,
        }
        if write_receipt:
            receipt_path = install_root / "install-receipt.json"
            atomic_write(
                receipt_path,
                (
                    json.dumps(
                        receipt,
                        ensure_ascii=False,
                        sort_keys=True,
                        indent=2,
                    )
                    + "\n"
                ).encode("utf-8"),
            )
        return receipt
    except Exception as exc:
        if not rollback_on_error:
            raise
        try:
            restore_runtime_rollback(install_root, backup_dir)
        except Exception as rollback_exc:
            raise HostBootstrapError(
                f"instalação falhou ({exc}); rollback falhou ({rollback_exc})"
            ) from exc
        raise


def default_install_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise HostBootstrapError("LOCALAPPDATA não definido")
    return Path(local) / "ReqSys" / "CommandGateway"


def _mingit_marker_matches(target: Path) -> bool:
    marker = target / "REQSYS-MINGIT-SOURCE.json"
    git_exe = target / "cmd" / "git.exe"
    if not marker.is_file() or not git_exe.is_file():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        data.get("version") == MINGIT_VERSION
        and data.get("source_url") == MINGIT_URL
        and data.get("source_sha256") == MINGIT_SHA256
        and data.get("source_size") == MINGIT_SIZE
    )


def provision_mingit(install_root: Path) -> Path:
    if sys.platform != "win32":
        raise HostBootstrapError("Git não encontrado no host e fallback MinGit só é permitido no Windows")
    vendor = install_root / "vendor"
    target = vendor / f"mingit-{MINGIT_VERSION}"
    if _mingit_marker_matches(target):
        return target / "cmd" / "git.exe"
    vendor.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="reqsys-mingit-") as tmp:
        temp_root = Path(tmp)
        archive = temp_root / "mingit.zip"
        extracted = temp_root / "extracted"
        download_verified_file(
            MINGIT_URL,
            archive,
            expected_sha256=MINGIT_SHA256,
            expected_size=MINGIT_SIZE,
            max_bytes=MAX_MINGIT_ARCHIVE_BYTES,
        )
        safe_extract_zip(archive, extracted)
        candidate = extracted / "cmd" / "git.exe"
        if not candidate.is_file():
            raise HostBootstrapError("MinGit verificado não contém cmd/git.exe")
        staged = vendor / f".mingit-{MINGIT_VERSION}-staged-{uuid.uuid4().hex}"
        if staged.exists():
            shutil.rmtree(staged)
        shutil.copytree(extracted, staged)
        marker = {
            "version": MINGIT_VERSION,
            "source_url": MINGIT_URL,
            "source_sha256": MINGIT_SHA256,
            "source_size": MINGIT_SIZE,
            "provisioned_at": utc_now(),
        }
        atomic_write(
            staged / "REQSYS-MINGIT-SOURCE.json",
            (json.dumps(marker, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        )
        if target.exists():
            shutil.rmtree(target)
        os.replace(staged, target)
    return target / "cmd" / "git.exe"


def resolve_git(install_root: Path) -> tuple[Path, str]:
    system_git = shutil.which("git")
    if system_git:
        return Path(system_git), "system"
    return provision_mingit(install_root), "mingit"


def _git_command(git_executable: Path | str | None = None) -> str:
    if git_executable is not None:
        candidate = Path(git_executable)
        if not candidate.is_file():
            raise HostBootstrapError(f"Git informado não existe: {candidate}")
        return str(candidate)
    git = shutil.which("git")
    if not git:
        raise HostBootstrapError("Git não encontrado no host")
    return git


def ensure_safe_directory(target: Path, git_executable: Path | str | None = None) -> None:
    git = _git_command(git_executable)
    resolved = str(target.resolve()).replace("\\", "/")
    listed = subprocess.run([git, "config", "--global", "--get-all", "safe.directory"], text=True,
                            capture_output=True, encoding="utf-8", errors="replace", check=False, timeout=20)
    if listed.returncode not in (0, 1):
        raise HostBootstrapError(f"leitura de safe.directory falhou: {listed.stderr[-1000:]}")
    existing = {line.strip().replace("\\", "/").casefold() for line in listed.stdout.splitlines() if line.strip()}
    if resolved.casefold() not in existing:
        added = subprocess.run([git, "config", "--global", "--add", "safe.directory", resolved], text=True,
                               capture_output=True, encoding="utf-8", errors="replace", check=False, timeout=20)
        if added.returncode != 0:
            raise HostBootstrapError(f"registro de safe.directory falhou: {added.stderr[-1000:]}")


def prepare_validation_repo(
    work_root: Path,
    commit: str,
    repository_url: str | None = None,
    git_executable: Path | str | None = None,
) -> tuple[Path, str]:
    commit = validate_commit(commit)
    git = _git_command(git_executable)
    target = work_root / "operational-rules-validation"
    source = repository_url or f"https://github.com/{REPOSITORY}.git"
    if target.exists() and not (target / ".git").exists():
        raise HostBootstrapError(f"destino de validação existe sem .git: {target}")
    if not target.exists():
        clone = subprocess.run([git, "clone", "--no-checkout", source, str(target)], text=True, capture_output=True, encoding="utf-8", errors="replace", check=False, timeout=120)
        if clone.returncode != 0:
            raise HostBootstrapError(f"clone de validação falhou: {clone.stderr[-1000:]}")
    ensure_safe_directory(target, git)
    fetch = subprocess.run([git, "-C", str(target), "fetch", "--no-tags", source, commit], text=True, capture_output=True, encoding="utf-8", errors="replace", check=False, timeout=120)
    if fetch.returncode != 0:
        raise HostBootstrapError(f"fetch da revisão de validação falhou: {fetch.stderr[-1000:]}")
    checkout = subprocess.run([git, "-C", str(target), "checkout", "--detach", commit], text=True, capture_output=True, encoding="utf-8", errors="replace", check=False, timeout=60)
    if checkout.returncode != 0:
        raise HostBootstrapError(f"checkout de validação falhou: {checkout.stderr[-1000:]}")
    head = subprocess.run([git, "-C", str(target), "rev-parse", "HEAD"], text=True, capture_output=True, encoding="utf-8", errors="replace", check=False, timeout=20)
    status = subprocess.run([git, "-C", str(target), "status", "--porcelain"], text=True, capture_output=True, encoding="utf-8", errors="replace", check=False, timeout=20)
    if head.returncode != 0 or status.returncode != 0 or head.stdout.strip().lower() != commit or status.stdout.strip():
        raise HostBootstrapError("repositório de validação não corresponde ao SHA aprovado ou não está limpo")
    return target, head.stdout.strip().lower()


def write_blocked_receipt(install_root: Path | None, commit: str | None, error_text: str) -> None:
    if install_root is None:
        return
    try:
        payload = {
            "result": "HOST_BOOTSTRAP_BLOCKED",
            "gateway_exit_code": EXIT_HOST_BOOTSTRAP,
            "source_commit": commit,
            "blocked_at": utc_now(),
            "error": error_text,
        }
        atomic_write(
            install_root / "install-receipt.json",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        )
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap inicial verificável do Command Gateway")
    parser.add_argument("--commit", required=True, help="SHA completo aprovado do repositório canônico")
    parser.add_argument("--expected-self-sha256", required=True, help="SHA-256 esperado deste próprio instalador")
    parser.add_argument("--install-root", type=Path, default=None)
    parser.add_argument("--work-root", type=Path, default=Path(r"C:\dev\chatgpt-workers"))
    ns = parser.parse_args()
    install_root: Path | None = None
    commit: str | None = None
    try:
        verify_self(ns.expected_self_sha256)
        commit = validate_commit(ns.commit)
        install_root = ns.install_root or default_install_root()
        with tempfile.TemporaryDirectory(prefix="reqsys-host-bootstrap-") as tmp:
            bundle = Path(tmp)
            download_bundle(commit, bundle)
            receipt = install_bundle(bundle, install_root, ns.work_root, commit)
        git_executable, git_source = resolve_git(install_root)
        validation_repo, validation_head = prepare_validation_repo(
            ns.work_root,
            commit,
            git_executable=git_executable,
        )
        receipt.update(
            {
                "result": "HOST_BOOTSTRAP_OK",
                "git_source": git_source,
                "git_executable": str(git_executable),
                "validation_repo": str(validation_repo),
                "validation_head": validation_head,
            }
        )
        atomic_write(
            install_root / "install-receipt.json",
            (json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        )
        emit_json(receipt)
        return 0
    except (HostBootstrapError, urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        error_text = str(exc)
        write_blocked_receipt(install_root, commit, error_text)
        error = {"result": "HOST_BOOTSTRAP_BLOCKED", "gateway_exit_code": EXIT_HOST_BOOTSTRAP, "error": error_text}
        emit_json(error, file=sys.stderr)
        return EXIT_HOST_BOOTSTRAP


if __name__ == "__main__":
    raise SystemExit(main())

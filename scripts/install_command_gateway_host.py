#!/usr/bin/env python3
"""Bootstrap mínimo e verificável para instalar o Command Gateway em host novo."""

from __future__ import annotations

import argparse
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


class HostBootstrapError(RuntimeError):
    pass


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
    return manifest, index


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".tmp-{uuid.uuid4().hex}")
    temp.write_bytes(payload)
    os.replace(temp, path)


def install_bundle(bundle_dir: Path, install_root: Path, work_root: Path, source_commit: str) -> dict[str, Any]:
    source_commit = validate_commit(source_commit)
    manifest, index = verify_bundle(bundle_dir)
    install_root.mkdir(parents=True, exist_ok=True)
    existing = [install_root / dest for dest in RUNTIME_MAP.values() if (install_root / dest).exists()]
    backup_dir: Path | None = None
    if existing:
        backup_dir = install_root / f"backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        for current in existing:
            relative = current.relative_to(install_root)
            target = backup_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(current, target)
    installed: dict[str, dict[str, Any]] = {}
    for source, dest in RUNTIME_MAP.items():
        payload = (bundle_dir / source).read_bytes()
        target = install_root / dest
        atomic_write(target, payload)
        installed[dest] = {"sha256": sha256_file(target), "size": target.stat().st_size}
    work_root.mkdir(parents=True, exist_ok=True)
    for source, dest in RUNTIME_MAP.items():
        expected = index[source]
        actual = installed[dest]
        if actual["sha256"] != str(expected.get("sha256", "")).lower() or actual["size"] != expected.get("size"):
            raise HostBootstrapError(f"verificação pós-instalação falhou: {dest}")
    receipt = {
        "result": "HOST_BOOTSTRAP_RUNTIME_INSTALLED",
        "repository": REPOSITORY,
        "source_commit": source_commit,
        "rules_version": manifest["version"],
        "installed_at": utc_now(),
        "host": socket.gethostname(),
        "install_root": str(install_root),
        "work_root": str(work_root),
        "backup_dir": str(backup_dir) if backup_dir else None,
        "files": installed,
    }
    receipt_path = install_root / "install-receipt.json"
    atomic_write(receipt_path, (json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    return receipt


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

#!/usr/bin/env python3
"""Inventário mínimo e relatório de compatibilidade sem instalar nada.

A coleta local deve ser acionada somente após sessão/Gateway governados.
A opção --inventory permite avaliação pré-instalação de evidência previamente
obtida por API/controlador autorizado, sem executar comandos no host novo.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import platform
import re
import shutil
import socket
from pathlib import Path
from typing import Any

from host_readiness import DEFAULT_CONTRACT, ReadinessError, evaluate, load_json

GIB = 1024 ** 3
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
INVENTORY_KEYS = frozenset({
    "schema_version", "hostname", "os", "architecture", "logical_cpus",
    "memory_gib", "disk_total_gib", "disk_free_gib", "tools_detected",
})
TOOLS = ("python", "git", "docker")


def _memory_gib() -> float | None:
    """Lê apenas a memória física total, sem coletar processos ou usuários."""
    try:
        if os.name == "nt":
            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(status)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
            return round(status.ullTotalPhys / GIB, 2)
        # /proc/meminfo contém só totais globais; não inclui dados pessoais.
        memory = Path("/proc/meminfo")
        if memory.is_file():
            for line in memory.read_text(encoding="ascii").splitlines():
                if line.startswith("MemTotal:"):
                    return round(int(line.split()[1]) * 1024 / GIB, 2)
    except (OSError, ValueError, AttributeError):
        return None
    return None


def collect_local() -> dict[str, Any]:
    """Sondas estritamente informativas: nunca executa subprocessos."""
    hostname = socket.gethostname().strip()
    if not HOSTNAME_RE.fullmatch(hostname):
        hostname = None
    try:
        disk = shutil.disk_usage(Path.home().anchor or "/")
        disk_total, disk_free = round(disk.total / GIB, 2), round(disk.free / GIB, 2)
    except (OSError, ValueError):
        disk_total, disk_free = None, None
    cpus = os.cpu_count()
    return {
        "schema_version": 1,
        "hostname": hostname,
        "os": platform.system() or "unknown",
        "architecture": platform.machine() or "unknown",
        "logical_cpus": cpus if isinstance(cpus, int) and cpus > 0 else None,
        "memory_gib": _memory_gib(),
        "disk_total_gib": disk_total,
        "disk_free_gib": disk_free,
        "tools_detected": {
            "python": True,
            "git": shutil.which("git") is not None,
            "docker": shutil.which("docker") is not None,
        },
    }


def _valid_number(value: Any, *, integer: bool = False) -> bool:
    if value is None:
        return True
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if not math.isfinite(value) or not (0 <= value <= 1_000_000_000):
        return False
    return not integer or (isinstance(value, int) and value >= 1)


def validate_inventory(value: dict[str, Any]) -> dict[str, Any]:
    """Rejeita atributos extras para impedir propagação de segredos ou IPs."""
    if not isinstance(value, dict) or set(value) != INVENTORY_KEYS:
        raise ReadinessError("inventory_schema_mismatch")
    if value["schema_version"] != 1 or isinstance(value["schema_version"], bool):
        raise ReadinessError("inventory_schema_version_invalid")
    hostname = value["hostname"]
    if hostname is not None and (
        not isinstance(hostname, str) or not HOSTNAME_RE.fullmatch(hostname)
    ):
        raise ReadinessError("inventory_hostname_invalid")
    for key in ("os", "architecture"):
        if not isinstance(value[key], str) or not 1 <= len(value[key]) <= 64:
            raise ReadinessError(f"inventory_{key}_invalid")
    for key in ("logical_cpus", "memory_gib", "disk_total_gib", "disk_free_gib"):
        if not _valid_number(value[key], integer=key == "logical_cpus"):
            raise ReadinessError(f"inventory_{key}_invalid")
    if (
        value["disk_total_gib"] is not None
        and value["disk_free_gib"] is not None
        and value["disk_free_gib"] > value["disk_total_gib"]
    ):
        raise ReadinessError("inventory_disk_inconsistent")
    tools = value["tools_detected"]
    if not isinstance(tools, dict) or set(tools) != set(TOOLS) or any(type(tools[k]) is not bool for k in TOOLS):
        raise ReadinessError("inventory_tools_invalid")
    return value


def build_report(
    contract: dict[str, Any],
    profile: str,
    inventory: dict[str, Any],
    evidence: dict[str, Any] | None = None,
    source_sha: str | None = None,
) -> dict[str, Any]:
    inv = validate_inventory(inventory)
    if source_sha is not None and not SHA_RE.fullmatch(source_sha):
        raise ReadinessError("source_sha_invalid")
    readiness = evaluate(contract, profile, evidence)
    observations: list[str] = []
    if inv["os"] not in ("Windows", "Linux"):
        observations.append("os_requires_manual_review")
    if not inv["tools_detected"]["git"]:
        observations.append("git_cli_not_detected")
    if not inv["tools_detected"]["docker"]:
        observations.append("docker_cli_not_detected_not_necessarily_required")
    if inv["memory_gib"] is None or inv["disk_total_gib"] is None:
        observations.append("hardware_capacity_incomplete")

    # Evidência declarativa de capability NÃO comprova E2E, versão nem runtime.
    if readiness["status"] == "blocked":
        status = "missing_required_capabilities"
    elif readiness["status"] == "needs_evidence":
        status = "needs_governed_capability_evidence"
    else:
        status = "awaiting_physical_e2e"
    return {
        "schema_version": 1,
        "type": "host_onboarding_assessment",
        "profile": profile,
        "source_sha": source_sha.lower() if source_sha else None,
        "inventory": inv,
        "compatibility": {
            "status": status,
            "observations": observations,
            "required_capabilities": readiness["required_capabilities"],
            "unconfirmed_capabilities": readiness["missing_capabilities"],
            "runtime_repository": readiness["runtime_repository"],
        },
        "ready_for_work": False,
        "install_authorized": False,
        "next_step": (
            "Obter evidências governadas das capabilities, executar bootstrap/preflight "
            "conforme regras canônicas e homologar E2E físico no SHA atual."
        ),
    }


def save_report(path: Path, serialized: str) -> None:
    """Replay do mesmo resultado é permitido, mas nunca sobrescreve evidência."""
    if path.is_symlink():
        raise ReadinessError("output_symlink_rejected")
    try:
        if path.exists():
            if not path.is_file() or path.read_text(encoding="utf-8") != serialized:
                raise ReadinessError("output_exists_with_different_content")
            return
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
    except OSError as exc:
        raise ReadinessError(f"output_io_error:{type(exc).__name__}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Inventário mínimo e compatibilidade de novo host.")
    parser.add_argument("--profile", required=True, choices=("noteri", "desktop-pc24x7"))
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--inventory", type=Path, help="JSON de inventário coletado por canal governado")
    inputs.add_argument("--collect-local", action="store_true", help="Somente após bootstrap/Gateway autorizado")
    parser.add_argument("--evidence", type=Path, help="Declaração governada de capabilities")
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--source-sha", help="SHA completo e aprovado da versão das regras")
    parser.add_argument("--output", type=Path, help="Caminho de relatório local protegido; sem overwrite")
    args = parser.parse_args()
    try:
        contract = load_json(args.contract)
        inv = collect_local() if args.collect_local else load_json(args.inventory)
        evidence = load_json(args.evidence) if args.evidence else None
        report = build_report(contract, args.profile, inv, evidence, args.source_sha)
        encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        if args.output:
            save_report(args.output, encoded)
        print(encoded, end="")
        return 1 if report["compatibility"]["status"] == "missing_required_capabilities" else 0
    except ReadinessError as exc:
        print(json.dumps({"status": "invalid", "ready_for_work": False, "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Amostra mínima de capacidade do host, sem Docker e sem dados pessoais.

Somente leitura. Execute através do Command Gateway após Session Launcher.
Uma amostra não homologa Linux, operação 24x7 ou migração de workloads.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import re
import shutil
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

GIB = 1024 ** 3
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")


def linux_memory_bytes() -> tuple[int, int] | None:
    try:
        rows = {}
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            parts = line.replace(":", " ").split()
            if len(parts) >= 3 and parts[0] in {"MemTotal", "MemAvailable"}:
                rows[parts[0]] = int(parts[1]) * 1024
        if rows.get("MemTotal", 0) > 0 and 0 <= rows.get("MemAvailable", -1) <= rows["MemTotal"]:
            return rows["MemTotal"], rows["MemAvailable"]
    except (OSError, ValueError):
        return None
    return None


def windows_memory_bytes() -> tuple[int, int] | None:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    try:
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(stat)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
        kernel.GlobalMemoryStatusEx.restype = ctypes.c_int
        if not kernel.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return None
        if not 0 <= stat.ullAvailPhys <= stat.ullTotalPhys or stat.ullTotalPhys == 0:
            return None
        return int(stat.ullTotalPhys), int(stat.ullAvailPhys)
    except (AttributeError, OSError, ValueError):
        return None


def linux_cpu_ticks() -> tuple[int, int] | None:
    try:
        first = Path("/proc/stat").read_text(encoding="ascii").splitlines()[0].split()
        if first[0] != "cpu" or len(first) < 5:
            return None
        ticks = [int(value) for value in first[1:9]]
        total = sum(ticks)
        idle = ticks[3] + (ticks[4] if len(ticks) > 4 else 0)
        return total, idle
    except (OSError, ValueError, IndexError):
        return None


def windows_cpu_ticks() -> tuple[int, int] | None:
    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]

    def number(value: FILETIME) -> int:
        return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)

    try:
        idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.GetSystemTimes.argtypes = [ctypes.POINTER(FILETIME)] * 3
        api.GetSystemTimes.restype = ctypes.c_int
        if not api.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        return number(kernel) + number(user), number(idle)
    except (OSError, AttributeError, ValueError):
        return None


def cpu_percent(sample_seconds: float, *, tick_reader=None, sleeper=time.sleep) -> float | None:
    reader = tick_reader or (windows_cpu_ticks if os.name == "nt" else linux_cpu_ticks)
    first = reader()
    if first is None:
        return None
    sleeper(sample_seconds)
    second = reader()
    if second is None:
        return None
    elapsed = second[0] - first[0]
    idle = second[1] - first[1]
    if elapsed <= 0 or idle < 0 or idle > elapsed:
        return None
    return round(100 * (elapsed - idle) / elapsed, 2)


def collect(*, sample_seconds: float = 0.5, source_sha: str) -> dict:
    if not 0.2 <= sample_seconds <= 5.0:
        raise ValueError("sample_seconds_out_of_range")
    if not SHA_RE.fullmatch(source_sha):
        raise ValueError("source_sha_invalid")

    host = socket.gethostname().strip()
    if not HOST_RE.fullmatch(host):
        raise ValueError("hostname_invalid")
    system = platform.system()
    mem = windows_memory_bytes() if system == "Windows" else (
        linux_memory_bytes() if system == "Linux" else None
    )
    try:
        disk = shutil.disk_usage(Path.home().anchor or "/")
        disk_total = round(disk.total / GIB, 2)
        disk_free = round(disk.free / GIB, 2)
    except (OSError, ValueError):
        disk_total = disk_free = None

    cpu = cpu_percent(sample_seconds)
    memory_total = round(mem[0] / GIB, 2) if mem else None
    memory_available = round(mem[1] / GIB, 2) if mem else None
    cpus = os.cpu_count()
    complete = (
        isinstance(cpus, int) and cpus > 0
        and cpu is not None
        and mem is not None
        and disk_total is not None
        and disk_free is not None
    )
    return {
        "schema_version": 1,
        "type": "native_host_capacity_sample",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "hostname": host,
        "os": system,
        "architecture": platform.machine(),
        "source_sha_claim": source_sha.lower(),
        "source_sha_must_be_validated_by_gateway": True,
        "logical_cpus": cpus,
        "cpu_sample_seconds": sample_seconds,
        "cpu_usage_percent": cpu,
        "memory_total_gib": memory_total,
        "memory_available_gib": memory_available,
        "disk_total_gib": disk_total,
        "disk_free_gib": disk_free,
        "capacity_sample_complete": complete,
        "ready_for_migration": False,
        "decision_reason": "requires_sustained_measurements_and_workload_baseline",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Amostra nativa limitada de capacidade, somente leitura")
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--sample-seconds", type=float, default=0.5)
    args = parser.parse_args()
    try:
        result = collect(sample_seconds=args.sample_seconds, source_sha=args.source_sha)
    except (OSError, ValueError) as exc:
        # Não imprimir dados externos nem paths sensíveis.
        print(json.dumps({"status": "blocked", "error_type": type(exc).__name__}))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["capacity_sample_complete"] else 3


if __name__ == "__main__":
    raise SystemExit(main())

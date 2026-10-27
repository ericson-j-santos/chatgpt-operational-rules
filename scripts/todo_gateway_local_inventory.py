#!/usr/bin/env python3
"""Inventário local, limitado e somente leitura para PostgreSQL/TODO Gateway.

O módulo não executa comandos, não consulta bancos e não lê arquivos de
credenciais. A importação não coleta nada. A coleta física só ocorre por
``main``/``collect`` e deve ser chamada pelo Command Gateway após preflight.
"""
from __future__ import annotations

import argparse
import ctypes
import ipaddress
import json
import ntpath
import os
import platform
import re
import socket
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Protocol

SCHEMA_VERSION = 1
POSTGRES_VERSIONS = tuple(str(value) for value in range(12, 19))
POSTGRES_SERVICE_NAMES = tuple(f"postgresql-x64-{value}" for value in POSTGRES_VERSIONS)
POSTGRES_COMPONENTS = ("postgres", "psql", "pg_isready")
POSTGRES_PORT = 5432
TODO_GATEWAY_PORT = 8000
ENVIRONMENT_KEYS = (
    "DATABASE_URL",
    "TODO_GATEWAY_TOKEN",
    "NOTION_TOKEN",
    "NOTION_DATA_SOURCE_ID",
)
MAX_TCP_TABLE_BYTES = 1024 * 1024
MAX_TCP_ROWS = 4096
MIB_TCP_STATE_LISTEN = 2
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class ProbeInputError(ValueError):
    """Entrada rejeitada antes de qualquer I/O."""


class ProbeUnavailable(RuntimeError):
    """Uma fonte readonly não pôde ser consultada."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ServiceRecord:
    name: str
    state: str
    pid: int | None


@dataclass(frozen=True)
class ListenerRecord:
    port: int
    pid: int
    bind_scope: str
    ip_version: int


class InventoryBackend(Protocol):
    def services(self) -> tuple[ServiceRecord, ...]: ...

    def postgres_executables(self) -> Mapping[str, tuple[str, ...]]: ...

    def listeners(self) -> tuple[ListenerRecord, ...]: ...

    def process_image(self, pid: int) -> str | None: ...

    def gateway_task_marker_present(self) -> bool: ...

    def gateway_runtime_pythons(self) -> tuple[str, ...]: ...


def _path_equal(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return ntpath.normcase(ntpath.normpath(left)) == ntpath.normcase(ntpath.normpath(right))


def _basename(value: str | None) -> str:
    return ntpath.basename(value or "").casefold()


def _bind_scope(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    if address.is_unspecified:
        return "wildcard"
    if address.is_loopback:
        return "loopback"
    return "non_loopback"


def _target_listener_port(state: int, raw_port: int) -> int | None:
    if state != MIB_TCP_STATE_LISTEN:
        return None
    port = socket.ntohs(raw_port & 0xFFFF)
    return port if port in (POSTGRES_PORT, TODO_GATEWAY_PORT) else None


def _validate_tcp_table_bounds(
    allocated_bytes: int,
    reported_bytes: int,
    row_count: int,
    row_size: int,
) -> None:
    if not 4 <= allocated_bytes <= MAX_TCP_TABLE_BYTES:
        raise ProbeUnavailable("tcp_table_unavailable")
    if not 4 <= reported_bytes <= allocated_bytes:
        raise ProbeUnavailable("tcp_table_bounds_exceeded")
    required = 4 + row_count * row_size
    if row_count > MAX_TCP_ROWS or required > allocated_bytes or required > reported_bytes:
        raise ProbeUnavailable("tcp_table_bounds_exceeded")


class WindowsInventoryBackend:
    """Backend Win32 sem shell, WMI, banco ou enumeração ampla."""

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        if os.name != "nt":
            raise ProbeUnavailable("windows_backend_unavailable")
        self.environment = dict(os.environ if environment is None else environment)

    def services(self) -> tuple[ServiceRecord, ...]:
        from ctypes import wintypes

        class SERVICE_STATUS_PROCESS(ctypes.Structure):
            _fields_ = [
                ("dwServiceType", wintypes.DWORD),
                ("dwCurrentState", wintypes.DWORD),
                ("dwControlsAccepted", wintypes.DWORD),
                ("dwWin32ExitCode", wintypes.DWORD),
                ("dwServiceSpecificExitCode", wintypes.DWORD),
                ("dwCheckPoint", wintypes.DWORD),
                ("dwWaitHint", wintypes.DWORD),
                ("dwProcessId", wintypes.DWORD),
                ("dwServiceFlags", wintypes.DWORD),
            ]

        states = {
            1: "stopped",
            2: "start_pending",
            3: "stop_pending",
            4: "running",
            5: "continue_pending",
            6: "pause_pending",
            7: "paused",
        }
        api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        api.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        api.OpenSCManagerW.restype = wintypes.HANDLE
        api.OpenServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD]
        api.OpenServiceW.restype = wintypes.HANDLE
        api.QueryServiceStatusEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_ubyte),
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        api.QueryServiceStatusEx.restype = wintypes.BOOL
        api.CloseServiceHandle.argtypes = [wintypes.HANDLE]
        api.CloseServiceHandle.restype = wintypes.BOOL
        manager = api.OpenSCManagerW(None, None, 0x0001)
        if not manager:
            raise ProbeUnavailable("service_manager_unavailable")
        records: list[ServiceRecord] = []
        try:
            for name in POSTGRES_SERVICE_NAMES:
                service = api.OpenServiceW(manager, name, 0x0004)
                if not service:
                    if ctypes.get_last_error() == 1060:
                        continue
                    raise ProbeUnavailable("service_query_unavailable")
                try:
                    status = SERVICE_STATUS_PROCESS()
                    needed = wintypes.DWORD()
                    ok = api.QueryServiceStatusEx(
                        service,
                        0,
                        ctypes.cast(ctypes.byref(status), ctypes.POINTER(ctypes.c_ubyte)),
                        ctypes.sizeof(status),
                        ctypes.byref(needed),
                    )
                    if not ok:
                        raise ProbeUnavailable("service_status_unavailable")
                    pid = int(status.dwProcessId) or None
                    records.append(ServiceRecord(name, states.get(status.dwCurrentState, "unknown"), pid))
                finally:
                    api.CloseServiceHandle(service)
        finally:
            api.CloseServiceHandle(manager)
        return tuple(records)

    def postgres_executables(self) -> Mapping[str, tuple[str, ...]]:
        raise ProbeUnavailable("filesystem_identity_not_activated")

    def listeners(self) -> tuple[ListenerRecord, ...]:
        if os.name != "nt":
            raise ProbeUnavailable("tcp_table_unavailable")
        from ctypes import wintypes

        class TCP4_ROW(ctypes.Structure):
            _fields_ = [
                ("state", wintypes.DWORD),
                ("local_addr", wintypes.DWORD),
                ("local_port", wintypes.DWORD),
                ("remote_addr", wintypes.DWORD),
                ("remote_port", wintypes.DWORD),
                ("pid", wintypes.DWORD),
            ]

        class TCP6_ROW(ctypes.Structure):
            _fields_ = [
                ("local_addr", ctypes.c_ubyte * 16),
                ("local_scope", wintypes.DWORD),
                ("local_port", wintypes.DWORD),
                ("remote_addr", ctypes.c_ubyte * 16),
                ("remote_scope", wintypes.DWORD),
                ("remote_port", wintypes.DWORD),
                ("state", wintypes.DWORD),
                ("pid", wintypes.DWORD),
            ]

        api = ctypes.WinDLL("Iphlpapi.dll", use_last_error=True)
        get_table = api.GetExtendedTcpTable
        get_table.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL, wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
        get_table.restype = wintypes.DWORD
        findings: list[ListenerRecord] = []
        for family, row_type, version in ((socket.AF_INET, TCP4_ROW, 4), (socket.AF_INET6, TCP6_ROW, 6)):
            size = wintypes.DWORD(0)
            rc = get_table(None, ctypes.byref(size), False, family, 3, 0)
            if rc not in (0, 122) or not 4 <= size.value <= MAX_TCP_TABLE_BYTES:
                raise ProbeUnavailable("tcp_table_unavailable")
            raw = ctypes.create_string_buffer(size.value)
            allocated_bytes = ctypes.sizeof(raw)
            reported_size = wintypes.DWORD(allocated_bytes)
            rc = get_table(raw, ctypes.byref(reported_size), False, family, 3, 0)
            if rc != 0:
                raise ProbeUnavailable("tcp_table_unavailable")
            count = struct.unpack_from("<I", raw.raw, 0)[0]
            _validate_tcp_table_bounds(
                allocated_bytes,
                int(reported_size.value),
                count,
                ctypes.sizeof(row_type),
            )
            for index in range(count):
                row = row_type.from_buffer_copy(raw.raw, 4 + index * ctypes.sizeof(row_type))
                port = _target_listener_port(int(row.state), int(row.local_port))
                if port is None:
                    continue
                if version == 4:
                    address = ipaddress.IPv4Address(int(row.local_addr).to_bytes(4, "little"))
                else:
                    address = ipaddress.IPv6Address(bytes(row.local_addr))
                findings.append(ListenerRecord(port, int(row.pid), _bind_scope(address), version))
        return tuple(findings)

    def process_image(self, pid: int) -> str | None:
        raise ProbeUnavailable("filesystem_identity_not_activated")

    def gateway_task_marker_present(self) -> bool:
        raise ProbeUnavailable("filesystem_identity_not_activated")

    def gateway_runtime_pythons(self) -> tuple[str, ...]:
        raise ProbeUnavailable("filesystem_identity_not_activated")


def _port_summary(
    records: tuple[ListenerRecord, ...],
    association: str,
    *,
    observed: bool,
    reason: str | None = None,
    association_observed: bool = True,
    association_reason: str | None = None,
) -> dict[str, object]:
    if not observed:
        return {
            "observation": "not_observed",
            "listener_count": None,
            "bind_scopes": None,
            "association_observation": "not_observed",
            "association": None,
            "association_reason": reason,
            "reason": reason,
        }
    return {
        "observation": "observed",
        "listener_count": len(records),
        "bind_scopes": sorted({item.bind_scope for item in records}),
        "association_observation": "observed" if association_observed else "not_observed",
        "association": association if association_observed else None,
        "association_reason": None if association_observed else association_reason,
        "reason": None,
    }


def collect(
    *,
    source_sha: str,
    backend: InventoryBackend | None = None,
    environment: Mapping[str, str] | None = None,
    platform_name: str | None = None,
) -> dict[str, object]:
    if not SHA_RE.fullmatch(source_sha):
        raise ProbeInputError("source_sha_invalid")
    system = platform_name or platform.system()
    if system != "Windows":
        return {
            "schema_version": SCHEMA_VERSION,
            "type": "todo_gateway_local_inventory",
            "status": "blocked",
            "reason": "windows_only_probe",
            "source_sha_claim": source_sha.lower(),
        }
    env = dict(os.environ if environment is None else environment)
    active_backend = backend or WindowsInventoryBackend(env)
    errors: list[str] = []

    def process_image(pid: int) -> tuple[str | None, bool, str | None]:
        try:
            image = active_backend.process_image(pid)
        except ProbeUnavailable as exc:
            errors.append(exc.code)
            return None, False, exc.code
        except OSError:
            errors.append("process_image_unavailable")
            return None, False, "process_image_unavailable"
        if image is None:
            errors.append("process_image_unavailable")
            return None, False, "process_image_unavailable"
        return image, True, None

    try:
        services = active_backend.services()
        services_observation = "observed"
        services_reason = None
    except (OSError, ProbeUnavailable) as exc:
        services = ()
        services_observation = "not_observed"
        services_reason = exc.code if isinstance(exc, ProbeUnavailable) else "service_query_unavailable"
        errors.append(services_reason)
    try:
        executables = active_backend.postgres_executables()
        executables_observation = "observed"
        executables_reason = None
    except (OSError, ProbeUnavailable) as exc:
        executables = {component: () for component in POSTGRES_COMPONENTS}
        executables_observation = "not_observed"
        executables_reason = exc.code if isinstance(exc, ProbeUnavailable) else "executable_inventory_unavailable"
        errors.append(executables_reason)
    try:
        listeners = active_backend.listeners()
        listeners_observation = "observed"
        listeners_reason = None
    except (OSError, ProbeUnavailable) as exc:
        listeners = ()
        listeners_observation = "not_observed"
        listeners_reason = exc.code if isinstance(exc, ProbeUnavailable) else "tcp_table_unavailable"
        errors.append(listeners_reason)

    pg_records = tuple(item for item in listeners if item.port == POSTGRES_PORT)
    pg_running_pids = {item.pid for item in services if item.state == "running" and item.pid}
    pg_known_images = set(executables.get("postgres", ()))
    pg_listener_pids = {item.pid for item in pg_records}
    pg_identity_observed = services_observation == "observed" and executables_observation == "observed"
    pg_identity_reason = services_reason if services_observation != "observed" else executables_reason
    pg_verified = False
    if listeners_observation != "observed":
        pg_association = "not_observed"
        pg_association_observed = False
        pg_association_reason = listeners_reason
    elif not pg_records:
        pg_association = "none_observed"
        pg_association_observed = True
        pg_association_reason = None
    elif not pg_identity_observed:
        pg_association = "not_observed"
        pg_association_observed = False
        pg_association_reason = pg_identity_reason
    elif len(pg_listener_pids) == 1:
        pid = next(iter(pg_listener_pids))
        image, image_observed, image_reason = process_image(pid)
        if not image_observed:
            pg_association = "not_observed"
            pg_association_observed = False
            pg_association_reason = image_reason
        else:
            pg_verified = pid in pg_running_pids and _basename(image) == "postgres.exe" and any(_path_equal(image, known) for known in pg_known_images)
            pg_association = "verified_postgresql_service" if pg_verified else "unverified"
            pg_association_observed = True
            pg_association_reason = None
    else:
        pg_association = "unverified"
        pg_association_observed = True
        pg_association_reason = None

    gateway_records = tuple(item for item in listeners if item.port == TODO_GATEWAY_PORT)
    gateway_pids = {item.pid for item in gateway_records}
    try:
        task_marker = active_backend.gateway_task_marker_present()
        task_observation = "observed"
        task_reason = None
    except (OSError, ProbeUnavailable) as exc:
        task_marker = None
        task_observation = "not_observed"
        task_reason = exc.code if isinstance(exc, ProbeUnavailable) else "scheduled_task_marker_unavailable"
        errors.append(task_reason)
    try:
        runtime_pythons = active_backend.gateway_runtime_pythons()
        runtime_observation = "observed"
        runtime_reason = None
    except (OSError, ProbeUnavailable) as exc:
        runtime_pythons = ()
        runtime_observation = "not_observed"
        runtime_reason = exc.code if isinstance(exc, ProbeUnavailable) else "gateway_runtime_unavailable"
        errors.append(runtime_reason)
    runtime_python_present = bool(runtime_pythons) if runtime_observation == "observed" else None
    gateway_identity_observed = task_observation == "observed" and runtime_observation == "observed"
    gateway_identity_reason = task_reason if task_observation != "observed" else runtime_reason
    gateway_candidate = False
    if listeners_observation != "observed":
        gateway_association = "not_observed"
        gateway_association_observed = False
        gateway_association_reason = listeners_reason
    elif not gateway_records:
        gateway_association = "none_observed"
        gateway_association_observed = True
        gateway_association_reason = None
    elif not gateway_identity_observed:
        gateway_association = "not_observed"
        gateway_association_observed = False
        gateway_association_reason = gateway_identity_reason
    elif len(gateway_pids) == 1 and task_marker is True and runtime_python_present is True:
        image, image_observed, image_reason = process_image(next(iter(gateway_pids)))
        if not image_observed:
            gateway_association = "not_observed"
            gateway_association_observed = False
            gateway_association_reason = image_reason
        else:
            gateway_candidate = any(_path_equal(image, candidate) for candidate in runtime_pythons) and any(
                item.bind_scope in {"loopback", "wildcard"} for item in gateway_records
            )
            gateway_association = "candidate_dedicated_runtime" if gateway_candidate else "unverified"
            gateway_association_observed = True
            gateway_association_reason = None
    else:
        gateway_association = "unverified"
        gateway_association_observed = True
        gateway_association_reason = None
    health = {
        "observation": "not_observed",
        "healthz": None,
        "readyz": None,
        "reason": "identity_not_verified" if gateway_records else "capability_not_activated",
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "type": "todo_gateway_local_inventory",
        "status": "ok" if not errors else "partial",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_sha_claim": source_sha.lower(),
        "source_sha_must_be_validated_by_gateway": True,
        "read_only": True,
        "environment_presence": {
            "observation": "observed_current_process",
            "values": {key: bool(env.get(key, "").strip()) for key in ENVIRONMENT_KEYS},
        },
        "postgresql": {
            "services": {
                "observation": services_observation,
                "items": (
                    [
                        {"name": item.name, "state": item.state, "pid_present": item.pid is not None}
                        for item in services
                    ]
                    if services_observation == "observed"
                    else None
                ),
                "scope": "known_service_names_only",
                "reason": services_reason,
            },
            "executables": {
                "observation": executables_observation,
                "components": (
                    {component: bool(executables.get(component)) for component in POSTGRES_COMPONENTS}
                    if executables_observation == "observed"
                    else {component: None for component in POSTGRES_COMPONENTS}
                ),
                "scope": "filesystem_identity_source_not_activated",
                "reason": executables_reason,
            },
            "port_5432": _port_summary(
                pg_records,
                pg_association,
                observed=listeners_observation == "observed",
                reason=listeners_reason,
                association_observed=pg_association_observed,
                association_reason=pg_association_reason,
            ),
        },
        "todo_gateway": {
            "scheduled_task_marker": {
                "observation": task_observation,
                "present": task_marker,
                "scope": "filesystem_identity_source_not_activated",
                "reason": task_reason,
            },
            "dedicated_runtime_python": {
                "observation": runtime_observation,
                "present": runtime_python_present,
                "scope": "filesystem_identity_source_not_activated",
                "reason": runtime_reason,
            },
            "port_8000": _port_summary(
                gateway_records,
                gateway_association,
                observed=listeners_observation == "observed",
                reason=listeners_reason,
                association_observed=gateway_association_observed,
                association_reason=gateway_association_reason,
            ),
            "health": health,
        },
        "errors": sorted(set(errors)),
        "limitations": [
            "no_database_connection",
            "no_service_or_task_start",
            "no_secret_values",
            "no_health_or_readiness_request",
            "filesystem_identity_not_activated",
            "absence_is_limited_to_named_sources",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Inventário readonly local de PostgreSQL/TODO Gateway")
    parser.add_argument("--source-sha", required=True)
    args = parser.parse_args()
    try:
        result = collect(source_sha=args.source_sha)
    except (ProbeInputError, ProbeUnavailable) as exc:
        result = {"schema_version": SCHEMA_VERSION, "status": "blocked", "reason": exc.args[0]}
        print(json.dumps(result, sort_keys=True))
        return 2
    except OSError:
        print(json.dumps({"schema_version": SCHEMA_VERSION, "status": "blocked", "reason": "os_probe_unavailable"}, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" else 3


if __name__ == "__main__":
    raise SystemExit(main())

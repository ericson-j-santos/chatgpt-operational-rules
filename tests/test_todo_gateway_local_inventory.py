"""Testes herméticos da sonda local PostgreSQL/TODO Gateway."""
from __future__ import annotations

import ctypes
import importlib.util
import json
import pathlib
import runpy
import sys
import unittest
from unittest.mock import patch

FILE = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "todo_gateway_local_inventory.py"
spec = importlib.util.spec_from_file_location("todo_gateway_local_inventory", FILE)
assert spec and spec.loader
probe = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = probe
spec.loader.exec_module(probe)

SHA = "a" * 40
PG_IMAGE = r"C:\Program Files\PostgreSQL\17\bin\postgres.exe"
GW_IMAGE = r"C:\Users\User\AppData\Local\ReqSys\TodoGlobal24x7\.venv\Scripts\python.exe"


class FakeBackend:
    def __init__(self):
        self.service_records = ()
        self.executable_records = {name: () for name in probe.POSTGRES_COMPONENTS}
        self.listener_records = ()
        self.images = {}
        self.task_marker = False
        self.runtime_pythons = ()

    def services(self):
        return self.service_records

    def postgres_executables(self):
        return self.executable_records

    def listeners(self):
        return self.listener_records

    def process_image(self, pid):
        return self.images.get(pid)

    def gateway_task_marker_present(self):
        return self.task_marker

    def gateway_runtime_pythons(self):
        return self.runtime_pythons


class TodoGatewayLocalInventoryTests(unittest.TestCase):
    def test_postgres_requires_service_pid_and_known_executable(self):
        backend = FakeBackend()
        backend.service_records = (probe.ServiceRecord("postgresql-x64-17", "running", 41),)
        backend.executable_records["postgres"] = (PG_IMAGE,)
        backend.listener_records = (probe.ListenerRecord(5432, 41, "loopback", 4),)
        backend.images[41] = PG_IMAGE
        result = probe.collect(source_sha=SHA, backend=backend, environment={}, platform_name="Windows")
        self.assertEqual(result["postgresql"]["port_5432"]["association"], "verified_postgresql_service")
        self.assertEqual(result["postgresql"]["services"]["observation"], "observed")
        self.assertTrue(result["postgresql"]["executables"]["components"]["postgres"])
        self.assertNotIn("pid", result["postgresql"]["services"]["items"][0])
        self.assertNotIn("Program Files", json.dumps(result))

    def test_unrelated_listener_is_not_attributed_to_postgres(self):
        backend = FakeBackend()
        backend.service_records = (probe.ServiceRecord("postgresql-x64-17", "running", 41),)
        backend.executable_records["postgres"] = (PG_IMAGE,)
        backend.listener_records = (probe.ListenerRecord(5432, 99, "wildcard", 4),)
        backend.images[99] = r"C:\Other\postgres.exe"
        result = probe.collect(source_sha=SHA, backend=backend, environment={}, platform_name="Windows")
        self.assertEqual(result["postgresql"]["port_5432"]["association"], "unverified")

    def test_known_absence_is_scoped_not_global(self):
        result = probe.collect(source_sha=SHA, backend=FakeBackend(), environment={}, platform_name="Windows")
        self.assertEqual(result["postgresql"]["services"]["observation"], "observed")
        self.assertEqual(result["postgresql"]["services"]["items"], [])
        self.assertEqual(result["postgresql"]["services"]["scope"], "known_service_names_only")
        self.assertEqual(result["postgresql"]["port_5432"]["association"], "none_observed")
        self.assertIn("absence_is_limited_to_named_sources", result["limitations"])

    def test_gateway_is_only_candidate_and_health_is_never_observed(self):
        backend = FakeBackend()
        backend.task_marker = True
        backend.runtime_pythons = (GW_IMAGE,)
        backend.listener_records = (probe.ListenerRecord(8000, 73, "loopback", 4),)
        backend.images[73] = GW_IMAGE
        result = probe.collect(source_sha=SHA, backend=backend, environment={}, platform_name="Windows")
        self.assertEqual(result["todo_gateway"]["port_8000"]["association"], "candidate_dedicated_runtime")
        self.assertEqual(
            result["todo_gateway"]["health"],
            {
                "observation": "not_observed",
                "healthz": None,
                "readyz": None,
                "reason": "identity_not_verified",
            },
        )

    def test_environment_uses_service_contract_and_reports_presence_only(self):
        sensitive_value = "should-never-appear-in-output"
        result = probe.collect(
            source_sha=SHA,
            backend=FakeBackend(),
            environment={
                "DATABASE_URL": sensitive_value,
                "TODO_GATEWAY_TOKEN": sensitive_value,
                "NOTION_TOKEN": sensitive_value,
            },
            platform_name="Windows",
        )
        encoded = json.dumps(result)
        self.assertNotIn(sensitive_value, encoded)
        self.assertEqual(
            result["environment_presence"]["values"],
            {
                "DATABASE_URL": True,
                "TODO_GATEWAY_TOKEN": True,
                "NOTION_TOKEN": True,
                "NOTION_DATA_SOURCE_ID": False,
            },
        )

    def test_invalid_sha_fails_before_backend(self):
        with self.assertRaises(probe.ProbeInputError):
            probe.collect(source_sha="main", backend=FakeBackend(), environment={}, platform_name="Windows")

    def test_backend_failure_is_not_observed_not_false(self):
        backend = FakeBackend()

        def fail():
            raise probe.ProbeUnavailable("tcp_table_unavailable")

        backend.listeners = fail
        result = probe.collect(source_sha=SHA, backend=backend, environment={}, platform_name="Windows")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["postgresql"]["port_5432"]["observation"], "not_observed")
        self.assertIsNone(result["postgresql"]["port_5432"]["listener_count"])
        self.assertEqual(result["errors"], ["tcp_table_unavailable"])

    def test_tcp_bounds_use_allocated_and_reported_lengths(self):
        probe._validate_tcp_table_bounds(256, 256, 2, 24)
        invalid = (
            (256, 300, 2, 24),
            (256, 40, 2, 24),
            (256, 256, probe.MAX_TCP_ROWS + 1, 24),
        )
        for values in invalid:
            with self.subTest(values=values):
                with self.assertRaisesRegex(probe.ProbeUnavailable, "tcp_table_bounds_exceeded"):
                    probe._validate_tcp_table_bounds(*values)

    def test_tcp_rows_must_be_listening_and_on_a_target_port(self):
        raw_postgres = probe.socket.htons(probe.POSTGRES_PORT)
        self.assertEqual(
            probe._target_listener_port(probe.MIB_TCP_STATE_LISTEN, raw_postgres),
            probe.POSTGRES_PORT,
        )
        self.assertIsNone(probe._target_listener_port(5, raw_postgres))
        self.assertIsNone(
            probe._target_listener_port(probe.MIB_TCP_STATE_LISTEN, probe.socket.htons(22))
        )

    def test_native_filesystem_identity_sources_are_disabled_before_io_even_for_mapped_drive(self):
        backend = object.__new__(probe.WindowsInventoryBackend)
        backend.environment = {
            "SystemRoot": r"Z:\Windows",
            "ProgramFiles": r"Z:\Program Files",
            "LOCALAPPDATA": r"Z:\Users\User\AppData\Local",
            "USERPROFILE": r"Z:\Users\User",
        }
        with (
            patch.object(ctypes, "WinDLL", side_effect=AssertionError("Win32 call"), create=True),
            patch.object(probe.os, "scandir", side_effect=AssertionError("filesystem call")),
            patch.object(pathlib.Path, "lstat", side_effect=AssertionError("filesystem call")),
        ):
            for method, args in (
                (backend.postgres_executables, ()),
                (backend.process_image, (123,)),
                (backend.gateway_task_marker_present, ()),
                (backend.gateway_runtime_pythons, ()),
            ):
                with self.subTest(method=method.__name__):
                    with self.assertRaisesRegex(
                        probe.ProbeUnavailable, "filesystem_identity_not_activated"
                    ):
                        method(*args)

    def test_tcp_observation_survives_disabled_identity(self):
        class IdentityDisabledBackend(FakeBackend):
            def postgres_executables(self):
                raise probe.ProbeUnavailable("filesystem_identity_not_activated")

            def process_image(self, pid):
                raise probe.ProbeUnavailable("filesystem_identity_not_activated")

            def gateway_task_marker_present(self):
                raise probe.ProbeUnavailable("filesystem_identity_not_activated")

            def gateway_runtime_pythons(self):
                raise probe.ProbeUnavailable("filesystem_identity_not_activated")

        backend = IdentityDisabledBackend()
        backend.service_records = (probe.ServiceRecord("postgresql-x64-17", "running", 41),)
        backend.listener_records = (probe.ListenerRecord(5432, 41, "loopback", 4),)
        result = probe.collect(source_sha=SHA, backend=backend, environment={}, platform_name="Windows")
        port = result["postgresql"]["port_5432"]
        self.assertEqual(port["observation"], "observed")
        self.assertEqual(port["listener_count"], 1)
        self.assertEqual(port["bind_scopes"], ["loopback"])
        self.assertEqual(port["association_observation"], "not_observed")
        self.assertIsNone(port["association"])
        self.assertEqual(port["association_reason"], "filesystem_identity_not_activated")
        self.assertEqual(result["status"], "partial")

    def test_import_has_no_physical_io(self):
        with (
            patch.object(ctypes, "WinDLL", side_effect=AssertionError("Win32 call"), create=True),
            patch.object(pathlib.Path, "lstat", side_effect=AssertionError("filesystem call")),
        ):
            namespace = runpy.run_path(str(FILE), run_name="todo_probe_import_only")
        self.assertIn("collect", namespace)

    def test_source_has_no_generic_execution_network_or_database_access(self):
        source = FILE.read_text(encoding="utf-8")
        for forbidden in (
            "subprocess",
            "os.system(",
            "shell=True",
            "urlopen",
            "build_opener",
            "HTTPRedirectHandler",
            "psql ",
            "SELECT ",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()

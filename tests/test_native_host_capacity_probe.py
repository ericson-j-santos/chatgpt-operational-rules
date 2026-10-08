"""Guardrails para o inventário de capacidade sem Docker."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

FILE = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "native_host_capacity_probe.py"
spec = importlib.util.spec_from_file_location("native_host_capacity_probe", FILE)
assert spec and spec.loader
probe = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = probe
spec.loader.exec_module(probe)

SHA = "a" * 40


class NativeHostCapacityProbeTests(unittest.TestCase):
    def test_cpu_sample_with_distinct_readback(self):
        values = iter([(100, 40), (200, 55)])
        slept = []
        self.assertEqual(
            probe.cpu_percent(0.5, tick_reader=lambda: next(values), sleeper=slept.append),
            85.0,
        )
        self.assertEqual(slept, [0.5])

    def test_cpu_sample_fails_closed_on_invalid_deltas(self):
        for ticks in ([(50, 30), (50, 30)], [(100, 40), (200, 201)]):
            values = iter(ticks)
            with self.subTest(ticks=ticks):
                self.assertIsNone(
                    probe.cpu_percent(0.5, tick_reader=lambda: next(values), sleeper=lambda _: None)
                )

    def test_linux_memory_and_cpu_parse(self):
        with patch.object(probe.Path, "read_text", return_value="MemTotal: 16000000 kB\nMemAvailable: 8000000 kB\n"):
            self.assertEqual(probe.linux_memory_bytes(), (16000000 * 1024, 8000000 * 1024))
        with patch.object(probe.Path, "read_text", return_value="cpu 100 2 3 400 5 6 7 8\nother line\n"):
            self.assertEqual(probe.linux_cpu_ticks(), (531, 405))

    def test_linux_memory_missing_available_is_unknown(self):
        with patch.object(probe.Path, "read_text", return_value="MemTotal: 16000000 kB\n"):
            self.assertIsNone(probe.linux_memory_bytes())

    def test_sanitized_snapshot_never_claims_host_ready(self):
        with (
            patch.object(probe.socket, "gethostname", return_value="DESKTOP-RP23OGS"),
            patch.object(probe.platform, "system", return_value="Windows"),
            patch.object(probe.platform, "machine", return_value="AMD64"),
            patch.object(probe, "windows_memory_bytes", return_value=(16 * probe.GIB, 7 * probe.GIB)),
            patch.object(probe, "cpu_percent", return_value=12.5),
            patch.object(probe.shutil, "disk_usage", return_value=SimpleNamespace(total=256 * probe.GIB, free=96 * probe.GIB)),
            patch.object(probe.os, "cpu_count", return_value=6),
        ):
            result = probe.collect(source_sha=SHA)
        self.assertTrue(result["capacity_sample_complete"])
        self.assertFalse(result["ready_for_migration"])
        self.assertTrue(result["source_sha_must_be_validated_by_gateway"])
        self.assertEqual(result["hostname"], "DESKTOP-RP23OGS")
        self.assertEqual(result["memory_available_gib"], 7.0)
        self.assertEqual(result["cpu_usage_percent"], 12.5)
        self.assertNotIn("ip", json.dumps(result).lower().split('"hostname"')[0])
        self.assertNotIn("username", result)
        self.assertNotIn("serial", result)

    def test_missing_cpu_does_not_become_success(self):
        with (
            patch.object(probe.socket, "gethostname", return_value="Noteri"),
            patch.object(probe.platform, "system", return_value="Windows"),
            patch.object(probe, "windows_memory_bytes", return_value=(8 * probe.GIB, 2 * probe.GIB)),
            patch.object(probe, "cpu_percent", return_value=None),
            patch.object(probe.shutil, "disk_usage", return_value=SimpleNamespace(total=256 * probe.GIB, free=80 * probe.GIB)),
        ):
            result = probe.collect(source_sha=SHA)
        self.assertFalse(result["capacity_sample_complete"])
        self.assertFalse(result["ready_for_migration"])

    def test_bad_sha_and_sample_window_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "source_sha_invalid"):
            probe.collect(source_sha="main")
        with self.assertRaisesRegex(ValueError, "sample_seconds_out_of_range"):
            probe.collect(source_sha=SHA, sample_seconds=60)

    def test_source_contains_no_shell_execution_or_docker_invocation(self):
        source = FILE.read_text(encoding="utf-8")
        for forbidden in ("subprocess", "os.system(", "shell=True", "docker ps", "docker inspect"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()

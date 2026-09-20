from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rdc_task_resilience as rtr


class FakeRepetition:
    Interval = ""
    Duration = ""
    StopAtDurationEnd = True


class FakeTrigger:
    def __init__(self, trigger_type: int) -> None:
        self.Type = trigger_type
        self.Enabled = False
        self.UserId = ""
        self.StartBoundary = ""
        self.DaysInterval = 0
        self.Repetition = FakeRepetition()


class FakeCollection:
    def __init__(self, factory) -> None:
        self._factory = factory
        self.items = []

    @property
    def Count(self) -> int:
        return len(self.items)

    def Create(self, item_type: int):
        item = self._factory(item_type)
        self.items.append(item)
        return item


class FakeSettings:
    Enabled = False
    StartWhenAvailable = False
    DisallowStartIfOnBatteries = True
    StopIfGoingOnBatteries = True
    ExecutionTimeLimit = "PT72H"
    MultipleInstances = 0
    RestartCount = 0
    RestartInterval = ""
    RunOnlyIfNetworkAvailable = True


class FakeDefinition:
    def __init__(self) -> None:
        self.RegistrationInfo = SimpleNamespace(Description="")
        self.Settings = FakeSettings()
        self.Principal = SimpleNamespace(UserId="", LogonType=0, RunLevel=1)
        self.Triggers = FakeCollection(FakeTrigger)
        self.Actions = FakeCollection(
            lambda _: SimpleNamespace(Path="", Arguments="")
        )


class FakeTask:
    def __init__(self, definition: FakeDefinition, path: str) -> None:
        self.Definition = definition
        self.Path = path
        self.run_count = 0

    def Run(self, _parameters: str):
        self.run_count += 1
        return SimpleNamespace(InstanceGuid="test-instance")


class FakeService:
    def NewTask(self, flags: int) -> FakeDefinition:
        return FakeDefinition()


class FakeFolder:
    def __init__(self) -> None:
        self.last_definition = None
        self.last_args = None
        self.last_task = None

    def RegisterTaskDefinition(self, *args):
        self.last_args = args
        self.last_definition = args[1]
        self.last_task = FakeTask(
            self.last_definition,
            rf"\Automation\{args[0]}",
        )
        return self.last_task


class RdcTaskResilienceTests(unittest.TestCase):
    def test_apply_builds_resilient_interactive_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            launcher = Path(tmp) / rtr.LAUNCHER.name
            launcher.write_text("@echo off\n", encoding="utf-8")
            folder = FakeFolder()
            with (
                mock.patch.object(rtr, "LAUNCHER", launcher),
                mock.patch.object(rtr, "identity", return_value=r"NOTERI\erics"),
                mock.patch.object(rtr, "connect", return_value=(FakeService(), folder)),
            ):
                result = rtr.apply(30)

        self.assertEqual(result["result"], "TASK_RESILIENCE_APPLIED")
        self.assertFalse(result["headless"])
        self.assertTrue(result["requires_user_logon"])
        self.assertFalse(result["start_requested"])
        self.assertTrue(result["enabled"])
        self.assertTrue(result["start_when_available"])
        self.assertFalse(result["disallow_start_on_battery"])
        self.assertFalse(result["stop_on_battery"])
        self.assertFalse(result["run_only_if_network_available"])
        self.assertEqual(result["execution_time_limit"], "PT0S")
        self.assertEqual(result["restart_count"], rtr.TASK_RESTART_COUNT)
        self.assertEqual(result["restart_interval"], rtr.TASK_RESTART_INTERVAL)
        self.assertEqual(result["restart_count"], 3)
        self.assertEqual(result["restart_interval"], "PT5M")
        self.assertEqual(
            result["principal_logon_type"],
            rtr.TASK_LOGON_INTERACTIVE_TOKEN,
        )
        self.assertEqual(result["trigger_count"], 2)
        self.assertEqual(result["action_count"], 1)
        self.assertEqual(
            folder.last_definition.Triggers.items[0].Type,
            rtr.TASK_TRIGGER_LOGON,
        )
        action = folder.last_definition.Actions.items[0]
        self.assertTrue(action.Path.lower().endswith("cmd.exe"))
        self.assertIn(str(launcher), action.Arguments)

    def test_headless_uses_startup_s4u_and_starts_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = root / "rdc-headless-runner.cjs"
            runner.write_text("// governed headless runner\n", encoding="utf-8")
            folder = FakeFolder()
            with (
                mock.patch.object(rtr, "HEADLESS_RUNNER", runner),
                mock.patch.object(rtr, "identity", return_value=r"DESKTOP\erics"),
                mock.patch.object(rtr, "is_admin", return_value=True),
                mock.patch.object(
                    rtr,
                    "resolve_node",
                    return_value=r"C:\Program Files\nodejs\node.exe",
                ),
                mock.patch.object(rtr, "connect", return_value=(FakeService(), folder)),
            ):
                result = rtr.apply(
                    30,
                    headless=True,
                    confirm=rtr.HEADLESS_CONFIRM,
                )

        self.assertEqual(
            result["result"],
            "TASK_RESILIENCE_HEADLESS_APPLIED",
        )
        self.assertTrue(result["headless"])
        self.assertFalse(result["requires_user_logon"])
        self.assertTrue(result["start_requested"])
        self.assertEqual(result["principal_logon_type"], rtr.TASK_LOGON_S4U)
        self.assertEqual(
            folder.last_definition.Triggers.items[0].Type,
            rtr.TASK_TRIGGER_BOOT,
        )
        self.assertEqual(folder.last_args[0], rtr.HEADLESS_TASK_NAME)
        self.assertEqual(folder.last_args[4], "")
        self.assertEqual(folder.last_args[5], rtr.TASK_LOGON_S4U)
        action = folder.last_definition.Actions.items[0]
        self.assertTrue(action.Path.lower().endswith("node.exe"))
        self.assertIn(str(runner), action.Arguments)
        self.assertEqual(folder.last_task.run_count, 1)

    def test_headless_requires_explicit_confirmation(self) -> None:
        with mock.patch.object(rtr, "is_admin", return_value=True):
            with self.assertRaisesRegex(PermissionError, "confirmação"):
                rtr.apply(30, headless=True, confirm="wrong")

    def test_headless_requires_admin(self) -> None:
        with mock.patch.object(rtr, "is_admin", return_value=False):
            with self.assertRaisesRegex(PermissionError, "elevação"):
                rtr.apply(
                    30,
                    headless=True,
                    confirm=rtr.HEADLESS_CONFIRM,
                )

    def test_apply_requires_launcher(self) -> None:
        with mock.patch.object(
            rtr,
            "LAUNCHER",
            Path(r"C:\definitely-missing\rdc.cmd"),
        ):
            with self.assertRaises(FileNotFoundError):
                rtr.apply(30)


if __name__ == "__main__":
    unittest.main()

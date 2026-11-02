"""Controles positivos, negativos e concorrencia do diagnostico Risk1 Noteri."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import noteri_state_dir_inventory as inv


class NoteriStateDirInventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.child = self.root / "sessions"
        self.child.mkdir()
        self.file = self.child / "state.json"
        self.file.write_text("test", encoding="utf-8")
        self.targets = (("sessions", self.child, "directory"), ("state_file", self.file, "file"))

    def test_two_identical_reads_pass_without_writing(self):
        result = inv.inspect_twice(self.targets)
        self.assertEqual(result["result"], "INSPECTION_OK")
        self.assertTrue(all(e["status"] == "PRESENT" for e in result["entries"]))
        self.assertFalse(result["mutated"])
        self.assertEqual(self.file.read_text(encoding="utf-8"), "test")

    def test_absent_component_does_not_create_it(self):
        path = self.root / "not-exists" / "child"
        result = inv.inspect_twice((("fixed", path, "directory"),))
        self.assertEqual(result["entries"][0]["status"], "MISSING")
        self.assertFalse(path.exists())

    def test_file_as_parent_is_rejected(self):
        result = inv.inspect_twice((("fixed", self.file / "child", "file"),))
        self.assertEqual(result["entries"][0]["status"], "NOT_DIRECTORY")

    def test_file_instead_of_directory_is_rejected(self):
        result = inv.inspect_twice((("fixed", self.file, "directory"),))
        self.assertEqual(result["entries"][0]["status"], "NOT_DIRECTORY")

    def test_directory_instead_of_file_is_rejected(self):
        result = inv.inspect_twice((("fixed", self.child, "file"),))
        self.assertEqual(result["entries"][0]["status"], "NOT_FILE")

    def test_windows_reparse_attribute_is_rejected(self):
        actual = os.lstat
        def altered(path):
            found = actual(path)
            if Path(path) == self.child:
                return SimpleNamespace(
                    st_mode=found.st_mode, st_file_attributes=inv.REPARSE_POINT,
                    st_dev=found.st_dev, st_ino=found.st_ino, st_size=found.st_size,
                    st_mtime_ns=found.st_mtime_ns,
                )
            return found
        result = inv.inspect_twice(self.targets, stat_fn=altered)
        self.assertEqual(result["entries"][0]["status"], "REPARSE")

    def test_symlink_is_rejected_without_following(self):
        link = self.root / "link"
        try:
            link.symlink_to(self.child, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink sem permissao neste runner")
        result = inv.inspect_twice((("fixed", link, "directory"),))
        self.assertEqual(result["entries"][0]["status"], "REPARSE")

    def test_change_between_reads_is_blocked(self):
        actual = os.lstat
        calls = 0
        count = len(self.child.parents) + 1
        def altered(path):
            nonlocal calls
            calls += 1
            found = actual(path)
            return SimpleNamespace(
                st_mode=found.st_mode,
                st_file_attributes=getattr(found, "st_file_attributes", 0),
                st_dev=found.st_dev, st_ino=found.st_ino, st_size=found.st_size,
                st_mtime_ns=found.st_mtime_ns + (1 if calls > count else 0),
            )
        result = inv.inspect_twice((("fixed", self.child, "directory"),), stat_fn=altered)
        self.assertEqual(result["result"], "BLOCKED_OBSERVATION_CHANGED")
        self.assertTrue(result["state_changed"])
        self.assertFalse(result["mutated"])

    def test_missing_target_changed_parent_between_reads_blocks(self):
        actual = os.lstat
        parent_reads = 0
        missing_target = self.root / "missing-child"

        def altered(path):
            nonlocal parent_reads
            found = actual(path)
            if Path(path) == self.root:
                parent_reads += 1
                if parent_reads == 2:
                    return SimpleNamespace(
                        st_mode=found.st_mode,
                        st_file_attributes=getattr(found, "st_file_attributes", 0),
                        st_dev=found.st_dev,
                        st_ino=found.st_ino,
                        st_size=found.st_size,
                        st_mtime_ns=found.st_mtime_ns + 1,
                    )
            return found

        result = inv.inspect_twice(
            (("missing", missing_target, "directory"),), stat_fn=altered
        )
        self.assertEqual(result["result"], "BLOCKED_OBSERVATION_CHANGED")
        self.assertTrue(result["state_changed"])
        self.assertFalse(result["mutated"])

    def test_missing_path_does_not_leak_full_path(self):
        import json
        result = inv.inspect_twice((("fixed", self.root / "personal", "directory"),))
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertNotIn("personal", json.dumps(result))

    def test_duplicate_target_names_block(self):
        items = (("x", self.child, "directory"), ("x", self.child, "directory"))
        self.assertEqual(inv.inspect_twice(items)["result"], "BLOCKED_TARGET_SET")

    def test_authorization_fail_closed(self):
        self.assertTrue(inv.authorized("NoTeRi", "nt", True, inv.CONFIRM))
        for args in (
            ("other", "nt", True, inv.CONFIRM),
            ("noteri", "posix", True, inv.CONFIRM),
            ("noteri", "nt", False, inv.CONFIRM),
            ("noteri", "nt", True, "UNEXPECTED"),
        ):
            self.assertFalse(inv.authorized(*args))

    def test_no_user_supplied_paths_on_cli(self):
        import inspect
        source = inspect.getsource(inv.main)
        for forbidden in ("--path", "--repo", "subprocess", "shell=True"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()

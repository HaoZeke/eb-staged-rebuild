"""A failed or interrupted privileged write must not block the next attempt."""

import errno
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from eb_staged_rebuild.atomicio import atomic_write
from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.faults import FaultInjector, InjectedCrash

def other_euid():
    return os.getuid() + 4242

class StaleTemporaryTests(unittest.TestCase):
    def module(self, directory):
        path = Path(directory) / "module.lua"
        path.write_text("old")
        path.chmod(0o640)
        return path, Path(directory) / ".module.lua.new"

    def test_metadata_refusal_removes_its_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path, temporary = self.module(directory)
            with patch("eb_staged_rebuild.atomicio.os.fchown",
                       side_effect=PermissionError(errno.EPERM, "owner denied")):
                with self.assertRaises(Refused):
                    atomic_write(path, "new", None, "replace")
            self.assertFalse(temporary.exists())
            self.assertEqual(path.read_text(), "old")
            atomic_write(path, "new", None, "replace")
            self.assertEqual(path.read_text(), "new")

    def test_fsync_error_removes_its_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path, temporary = self.module(directory)
            with patch("eb_staged_rebuild.atomicio.os.fsync", side_effect=OSError(errno.EIO, "I/O")):
                with self.assertRaises(OSError):
                    atomic_write(path, "new", None, "replace")
            self.assertFalse(temporary.exists())
            self.assertEqual(path.read_text(), "old")

    def test_a_temporary_file_replaced_by_another_writer_is_not_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            path, temporary = self.module(directory)

            def swap(fd):
                os.rename(temporary, Path(directory) / "kept")
                temporary.write_text("someone else")
                raise OSError(errno.EIO, "I/O")

            with patch("eb_staged_rebuild.atomicio.os.fsync", side_effect=swap):
                with self.assertRaises(OSError):
                    atomic_write(path, "new", None, "replace")
            self.assertEqual(temporary.read_text(), "someone else")
            self.assertEqual(path.read_text(), "old")

    def test_privileged_resume_accepts_a_temporary_owned_by_the_module_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            path, temporary = self.module(directory)
            with patch("eb_staged_rebuild.atomicio.os.geteuid", return_value=other_euid()):
                with self.assertRaises(InjectedCrash):
                    atomic_write(path, "partial", FaultInjector("replace.rename"), "replace")
                self.assertEqual(temporary.lstat().st_uid, path.lstat().st_uid)
                atomic_write(path, "new", None, "replace")
            self.assertEqual(path.read_text(), "new")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
            self.assertFalse(temporary.exists())

    def test_privileged_write_refuses_a_foreign_temporary_without_a_metadata_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "new.lua"
            temporary = Path(directory) / ".new.lua.new"
            temporary.write_text("foreign")
            with patch("eb_staged_rebuild.atomicio.os.geteuid", return_value=other_euid()):
                with self.assertRaisesRegex(Refused, "owned regular file"):
                    atomic_write(path, "new", None, "replace")
            self.assertEqual(temporary.read_text(), "foreign")
            self.assertFalse(path.exists())

    def test_privileged_write_still_refuses_a_hardlinked_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            path, temporary = self.module(directory)
            outside = Path(directory) / "outside"
            outside.write_text("protected")
            os.link(outside, temporary)
            with patch("eb_staged_rebuild.atomicio.os.geteuid", return_value=other_euid()):
                with self.assertRaisesRegex(Refused, "owned regular file"):
                    atomic_write(path, "new", None, "replace")
            self.assertEqual(outside.read_text(), "protected")
            self.assertEqual(path.read_text(), "old")

    def test_privileged_write_refuses_a_temporary_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path, temporary = self.module(directory)
            temporary.mkdir()
            with patch("eb_staged_rebuild.atomicio.os.geteuid", return_value=other_euid()):
                with self.assertRaisesRegex(Refused, "owned regular file"):
                    atomic_write(path, "new", None, "replace")
            self.assertTrue(temporary.is_dir())
            self.assertEqual(path.read_text(), "old")

if __name__ == "__main__":
    unittest.main()

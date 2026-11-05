"""POSIX access ACLs through the kernel, with faults injected at the descriptor calls."""

import errno
import os
from pathlib import Path
import stat
import struct
import tempfile
import unittest
from unittest.mock import patch

from eb_staged_rebuild.atomicio import atomic_write
from eb_staged_rebuild.errors import Refused

POSIX = "system.posix_acl_access"
USER_OBJ, USER, GROUP_OBJ, MASK, OTHER = 0x01, 0x02, 0x04, 0x10, 0x20
UNDEFINED = 0xffffffff
REAL = {name: getattr(os, name) for name in ("listxattr", "getxattr", "setxattr", "removexattr", "fchmod")}

def acl(user_obj, named, group_obj, mask, other):
    entries = [(USER_OBJ, user_obj, UNDEFINED), (USER, named, 65534),
               (GROUP_OBJ, group_obj, UNDEFINED), (MASK, mask, UNDEFINED), (OTHER, other, UNDEFINED)]
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)

def fd_path(fd):
    return os.readlink(f"/proc/self/fd/{fd}")

class Spy:
    def __init__(self):
        self.calls = []
        self.hooks = {}

    def wrap(self, name):
        def call(target, *args):
            if not isinstance(target, int):
                raise AssertionError(f"{name} by path: {target!r}")
            self.calls.append((name, fd_path(target), args[0] if args else None))
            hook = self.hooks.get(name)
            if hook is not None:
                hook(target, *args)
            return REAL[name](target, *args)

        return call

    def installed(self):
        from contextlib import ExitStack

        stack = ExitStack()
        for name in REAL:
            stack.enter_context(patch.object(os, name, self.wrap(name)))
        return stack

class PosixAclDescriptorTests(unittest.TestCase):
    def module(self, directory, value, mode):
        path = Path(directory) / "module.lua"
        path.write_text("old")
        path.chmod(mode)
        os.setxattr(path, POSIX, value)
        return path, os.getxattr(path, POSIX)

    def test_mask_wider_than_group_owner_round_trips_with_its_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path, stored = self.module(directory, acl(6, 6, 4, 6, 0), 0o660)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o660)
            atomic_write(path, "new", None, "replace")
            self.assertEqual(os.getxattr(path, POSIX), stored)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o660)

    def test_mask_narrower_than_group_owner_round_trips_with_its_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path, stored = self.module(directory, acl(6, 6, 6, 4, 4), 0o644)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
            atomic_write(path, "new", None, "replace")
            self.assertEqual(os.getxattr(path, POSIX), stored)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)

    def test_requested_mode_that_contradicts_the_mask_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path, stored = self.module(directory, acl(6, 6, 4, 6, 0), 0o660)
            with self.assertRaisesRegex(Refused, "ownership or mode did not round-trip"):
                atomic_write(path, "new", None, "replace", mode=0o600)
            self.assertEqual(path.read_text(), "old")
            self.assertEqual(os.getxattr(path, POSIX), stored)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o660)

    def test_acl_is_written_through_the_descriptor_after_ownership_and_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path, stored = self.module(directory, acl(6, 4, 4, 4, 0), 0o640)
            spy = Spy()
            with spy.installed(), patch("eb_staged_rebuild.atomicio.os.fchown",
                                        side_effect=lambda fd, *ids: spy.calls.append(
                                            ("fchown", fd_path(fd), ids))):
                atomic_write(path, "new", None, "replace")
            temporary = [name for name, target, _ in spy.calls if target.endswith(".module.lua.new")]
            self.assertLess(temporary.index("fchown"), temporary.index("fchmod"))
            self.assertLess(temporary.index("fchmod"), temporary.index("setxattr"))
            self.assertIn(("listxattr", str(path), None), spy.calls)
            self.assertEqual(os.getxattr(path, POSIX), stored)

    def test_acl_removed_between_list_and_read_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path, stored = self.module(directory, acl(6, 4, 4, 4, 0), 0o640)
            spy = Spy()

            def race(fd, name):
                if fd_path(fd) == str(path) and name == POSIX:
                    REAL["removexattr"](str(path), POSIX)

            spy.hooks["getxattr"] = race
            with spy.installed():
                with self.assertRaisesRegex(Refused, "listed but is no longer readable"):
                    atomic_write(path, "new", None, "replace")
            self.assertEqual(path.read_text(), "old")
            self.assertFalse((Path(directory) / ".module.lua.new").exists())

    def test_acl_replaced_between_reads_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path, stored = self.module(directory, acl(6, 4, 4, 4, 0), 0o640)
            spy = Spy()
            reads = []

            def race(fd, name):
                if fd_path(fd) == str(path) and name == POSIX:
                    reads.append(name)
                    if len(reads) == 2:
                        REAL["setxattr"](str(path), POSIX, acl(6, 0, 4, 4, 0))

            spy.hooks["getxattr"] = race
            with spy.installed():
                with self.assertRaisesRegex(Refused, "changed during inspection"):
                    atomic_write(path, "new", None, "replace")
            self.assertEqual(path.read_text(), "old")

    def test_acl_changed_before_rename_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path, stored = self.module(directory, acl(6, 4, 4, 4, 0), 0o640)
            changed = acl(6, 6, 4, 6, 0)

            class Faults:
                def reach(self, step):
                    if step == "replace.fsync":
                        os.setxattr(path, POSIX, changed)

            with self.assertRaisesRegex(Refused, "changed before"):
                atomic_write(path, "new", Faults(), "replace")
            self.assertEqual(path.read_text(), "old")
            self.assertNotEqual(os.getxattr(path, POSIX), stored)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o660)
            self.assertFalse((Path(directory) / ".module.lua.new").exists())

    def test_denied_descriptor_operations_are_refused(self):
        for operation, code in (("listxattr", errno.EACCES), ("listxattr", errno.ENOTSUP),
                                ("getxattr", errno.EACCES), ("getxattr", errno.EPERM),
                                ("setxattr", errno.EACCES), ("setxattr", errno.EPERM),
                                ("fchmod", errno.EPERM)):
            with self.subTest(operation=operation, errno=errno.errorcode[code]), \
                    tempfile.TemporaryDirectory() as directory:
                path, stored = self.module(directory, acl(6, 4, 4, 4, 0), 0o640)
                spy = Spy()

                def deny(fd, *args):
                    raise OSError(code, os.strerror(code))

                spy.hooks[operation] = deny
                with spy.installed():
                    with self.assertRaises(Refused):
                        atomic_write(path, "new", None, "replace")
                self.assertEqual(path.read_text(), "old")
                self.assertEqual(os.getxattr(path, POSIX), stored)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
                self.assertFalse((Path(directory) / ".module.lua.new").exists())

    def test_inherited_default_acl_removal_failure_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "module.lua"
            path.write_text("old")
            os.setxattr(root, "system.posix_acl_default", acl(6, 4, 4, 4, 0))
            spy = Spy()

            def deny(fd, name):
                raise OSError(errno.EPERM, os.strerror(errno.EPERM))

            spy.hooks["removexattr"] = deny
            with spy.installed():
                with self.assertRaisesRegex(Refused, "cannot preserve access metadata"):
                    atomic_write(path, "new", None, "replace")
            self.assertEqual(path.read_text(), "old")
            with self.assertRaises(OSError) as failure:
                os.getxattr(path, POSIX)
            self.assertEqual(failure.exception.errno, errno.ENODATA)
            self.assertFalse((root / ".module.lua.new").exists())

if __name__ == "__main__":
    unittest.main()

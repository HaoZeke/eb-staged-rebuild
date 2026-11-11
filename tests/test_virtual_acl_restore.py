"""Restore required ACL bytes before removing inherited virtual ACL views."""
import errno
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest

from eb_staged_rebuild.atomicio import atomic_write
from eb_staged_rebuild.errors import Refused
from tests.acl_fakes import AclModel, NFS4, OPAQUE, POSIX


class VirtualNfs4Model(AclModel):
    """A named inode exposes a mode-derived view until a POSIX ACL replaces it."""

    def __init__(self, replace_virtual=True):
        super().__init__("virtual-nfs4-with-posix-access", POSIX, {POSIX, NFS4}, chmod_rewrites=False)
        self.replace_virtual = replace_virtual

    def open(self, path, flags, *args, **kwargs):
        fd = super().open(path, flags, *args, **kwargs)
        if flags & os.O_CREAT and flags & os.O_EXCL:
            self._attrs(fd)[NFS4] = b"mode-derived:600"
        return fd

    def listxattr(self, fd):
        return [name for name in super().listxattr(fd) if name != NFS4]

    def fchmod(self, fd, mode):
        super().fchmod(fd, mode)
        attrs = self._attrs(fd)
        if NFS4 in attrs:
            attrs[NFS4] = b"mode-derived:%o" % mode

    def setxattr(self, fd, name, value, *args):
        super().setxattr(fd, name, value, *args)
        if name == POSIX and self.replace_virtual:
            self._attrs(fd).pop(NFS4, None)

    def removexattr(self, fd, name):
        if name == NFS4:
            self._hook("removexattr", fd, name)
            raise PermissionError(errno.EPERM, os.strerror(errno.EPERM))
        return super().removexattr(fd, name)


class VirtualAclRestoreTests(unittest.TestCase):
    def test_required_posix_acl_replaces_an_unremovable_virtual_view(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "module.lua"
            path.write_text("old")
            path.chmod(0o640)
            before = path.stat()
            model = VirtualNfs4Model()
            with model.installed():
                model.set_path(path, POSIX, OPAQUE)
                expected = model.get_path(path)
                atomic_write(path, "new", None, "replace")
                self.assertEqual(path.read_text(), "new")
                self.assertEqual(model.get_path(path), expected)
                self.assertEqual(model.get_path(path), {POSIX: OPAQUE})
                after = path.stat()
                self.assertEqual((after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)),
                                 (before.st_uid, before.st_gid, 0o640))
                writes = model.ops("setxattr")
                self.assertEqual([name for _, name in writes], [POSIX])
                self.assertEqual(model.ops("removexattr"), [])
                temporary = [kind for kind, target, _ in model.calls if target.endswith(".new")]
                self.assertLess(temporary.index("fchmod"), temporary.index("setxattr"))
                self.assertFalse((path.parent / ".module.lua.new").exists())

    def test_virtual_view_that_survives_required_acl_restoration_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "module.lua"
            path.write_text("old")
            path.chmod(0o640)
            model = VirtualNfs4Model(replace_virtual=False)
            reached = []
            with model.installed():
                model.set_path(path, POSIX, OPAQUE)
                expected = model.get_path(path)
                with self.assertRaisesRegex(Refused, "cannot preserve access metadata"):
                    atomic_write(path, "new", SimpleNamespace(reach=reached.append), "replace")
                self.assertEqual(path.read_text(), "old")
                self.assertEqual(model.get_path(path), expected)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
                temporary = [kind for kind, target, _ in model.calls if target.endswith(".new")]
                self.assertLess(temporary.index("setxattr"), temporary.index("removexattr"))
                self.assertNotIn("replace.rename", reached)
                self.assertFalse((path.parent / ".module.lua.new").exists())

    def test_absent_source_acl_does_not_accept_an_unremovable_virtual_view(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "module.lua"
            path.write_text("old")
            path.chmod(0o640)
            model = VirtualNfs4Model()
            with model.installed():
                self.assertEqual(model.get_path(path), {})
                with self.assertRaisesRegex(Refused, "cannot preserve access metadata"):
                    atomic_write(path, "new", None, "replace")
                self.assertEqual(path.read_text(), "old")
                self.assertEqual(model.get_path(path), {})
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
                self.assertTrue(model.ops("removexattr"))
                self.assertFalse((path.parent / ".module.lua.new").exists())


if __name__ == "__main__":
    unittest.main()

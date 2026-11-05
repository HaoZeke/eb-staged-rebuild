from contextlib import contextmanager
import errno
import os
from pathlib import Path
import stat
import tempfile
import unittest

from eb_staged_rebuild.atomicio import atomic_write
from eb_staged_rebuild.errors import Refused
from tests.acl_fakes import GPFS, NFS4, OPAQUE, POSIX, fd_path, models, raise_errno

class Hooked:
    """FaultInjector stand-in that runs a callable when a step is reached."""

    def __init__(self, actions):
        self.actions = actions
        self.reached = []

    def reach(self, step):
        self.reached.append(step)
        action = self.actions.get(step)
        if action is not None:
            action()

def only(target, hook):
    def filtered(fd, name=None):
        if fd_path(fd) == str(target):
            return hook(fd, name)
        return None

    return filtered

def on_temporary(hook):
    def filtered(fd, name=None):
        if fd_path(fd).endswith(".new"):
            return hook(fd, name)
        return None

    return filtered

class AclModelTests(unittest.TestCase):
    @contextmanager
    def scenario(self, model):
        with self.subTest(model=model.label), tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "module.lua"
            path.write_text("old")
            path.chmod(0o640)
            with model.installed():
                model.set_path(path, model.native, OPAQUE)
                yield path, model.get_path(path)

    def assert_unchanged(self, model, path, expected):
        self.assertEqual(path.read_text(), "old")
        self.assertEqual(model.get_path(path), expected)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        self.assertFalse((path.parent / ".module.lua.new").exists())

    def test_native_bytes_round_trip_through_descriptors_after_chmod(self):
        for model in models():
            with self.scenario(model) as (path, expected):
                before = path.stat()
                atomic_write(path, "new", None, "replace")
                after = path.stat()
                self.assertEqual(path.read_text(), "new")
                self.assertEqual(model.get_path(path), expected)
                self.assertEqual(model.get_path(path)[model.native], OPAQUE)
                self.assertEqual((after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)),
                                 (before.st_uid, before.st_gid, 0o640))
                writes = model.ops("setxattr")
                self.assertEqual([name for _, name in writes], [model.native])
                kinds = [kind for kind, target, _ in model.calls if target.endswith(".new")]
                self.assertLess(kinds.index("fchmod"), kinds.index("setxattr"))

    def test_projection_is_never_written_in_place_of_the_native_acl(self):
        model = models()[1]
        with tempfile.TemporaryDirectory() as directory, model.installed():
            path = Path(directory) / "module.lua"
            path.write_text("old")
            model.set_path(path, GPFS, OPAQUE)
            atomic_write(path, "new", None, "replace")
            attrs = model.get_path(path)
            self.assertEqual(attrs[GPFS], OPAQUE)
            self.assertEqual(attrs[NFS4], b"generic:" + OPAQUE)
            self.assertEqual(attrs[POSIX], b"posix-projection:" + OPAQUE)
            self.assertNotIn(POSIX, [name for _, name in model.ops("setxattr")])
            self.assertNotIn(NFS4, [name for _, name in model.ops("setxattr")])

    def test_projection_that_disagrees_with_the_native_acl_is_refused(self):
        model = models()[1]
        with tempfile.TemporaryDirectory() as directory, model.installed():
            path = Path(directory) / "module.lua"
            path.write_text("old")
            model.set_path(path, GPFS, OPAQUE)
            model.set_path(path, POSIX, b"independent posix acl")
            expected = model.get_path(path)
            with self.assertRaisesRegex(Refused, "did not round-trip"):
                atomic_write(path, "new", None, "replace")
            self.assertEqual(path.read_text(), "old")
            self.assertEqual(model.get_path(path), expected)

    def test_denied_list_is_refused_not_treated_as_absence(self):
        for model in models():
            with self.scenario(model) as (path, expected):
                model.hooks["listxattr"] = only(path, raise_errno(errno.EACCES))
                with self.assertRaisesRegex(Refused, "Permission denied"):
                    atomic_write(path, "new", None, "replace")
                model.hooks.clear()
                self.assert_unchanged(model, path, expected)

    def test_unsupported_list_is_refused(self):
        for model in models():
            with self.scenario(model) as (path, expected):
                for code in (errno.ENOTSUP, errno.EOPNOTSUPP):
                    model.hooks["listxattr"] = only(path, raise_errno(code))
                    with self.assertRaises(Refused):
                        atomic_write(path, "new", None, "replace")
                    model.hooks.clear()
                    self.assert_unchanged(model, path, expected)

    def test_denied_read_of_a_listed_acl_is_refused(self):
        for model in models():
            with self.scenario(model) as (path, expected):
                for code in (errno.EACCES, errno.EPERM, errno.EIO):
                    model.hooks["getxattr"] = only(path, raise_errno(code))
                    with self.assertRaises(Refused):
                        atomic_write(path, "new", None, "replace")
                    model.hooks.clear()
                    self.assert_unchanged(model, path, expected)

    def test_listed_then_unreadable_acl_is_refused(self):
        for model in models():
            with self.scenario(model) as (path, expected):
                model.hooks["getxattr"] = only(path, raise_errno(errno.ENODATA))
                with self.assertRaisesRegex(Refused, "listed but is no longer readable"):
                    atomic_write(path, "new", None, "replace")
                model.hooks.clear()
                self.assert_unchanged(model, path, expected)

    def test_denied_probe_of_an_unlisted_acl_name_is_refused(self):
        for model in models():
            with self.scenario(model) as (path, expected):
                listed = set(expected)

                def probe(fd, name):
                    if name not in listed:
                        raise OSError(errno.EACCES, os.strerror(errno.EACCES))
                    return None

                model.hooks["getxattr"] = only(path, probe)
                if listed == {GPFS, NFS4, POSIX}:
                    atomic_write(path, "new", None, "replace")
                    self.assertEqual(model.get_path(path), expected)
                    continue
                with self.assertRaises(Refused):
                    atomic_write(path, "new", None, "replace")
                model.hooks.clear()
                self.assert_unchanged(model, path, expected)

    def test_readable_virtual_acl_is_preserved_when_listing_is_empty(self):
        for model in models():
            with self.scenario(model) as (path, expected):
                info = path.stat()
                model.hooks["listxattr"] = only(path, lambda fd, name: [])
                atomic_write(path, "new", None, "replace")
                model.hooks.clear()
                self.assertEqual(path.read_text(), "new")
                self.assertEqual(model.get_path(path), expected)
                self.assertEqual(model.get_path(path)[model.native], OPAQUE)
                restored = path.stat()
                self.assertEqual((restored.st_uid, restored.st_gid, stat.S_IMODE(restored.st_mode)),
                                 (info.st_uid, info.st_gid, 0o640))
                self.assertFalse((path.parent / ".module.lua.new").exists())

    def test_unknown_system_attribute_is_refused(self):
        for model in models():
            with self.scenario(model) as (path, expected):
                model.hooks["listxattr"] = only(path, lambda fd, name: [*expected, "system.richacl"])
                with self.assertRaisesRegex(Refused, "unsupported access-control attribute system.richacl"):
                    atomic_write(path, "new", None, "replace")
                model.hooks.clear()
                self.assert_unchanged(model, path, expected)

    def test_attributes_changed_during_inspection_are_refused(self):
        for model in models():
            with self.scenario(model) as (path, expected):
                lists = []

                def changing(fd, name):
                    lists.append(fd)
                    if len(lists) == 2:
                        model.set_path(path, model.native, b"other")
                    return None

                model.hooks["listxattr"] = only(path, changing)
                with self.assertRaisesRegex(Refused, "changed during inspection"):
                    atomic_write(path, "new", None, "replace")
                model.hooks.clear()
                model.set_path(path, model.native, OPAQUE)
                self.assert_unchanged(model, path, expected)

    def test_ownership_change_during_inspection_is_refused(self):
        for model in models():
            with self.scenario(model) as (path, expected):
                def touch(fd, name):
                    os.chmod(path, 0o600)
                    return None

                model.hooks["listxattr"] = only(path, touch)
                with self.assertRaisesRegex(Refused, "changed during inspection"):
                    atomic_write(path, "new", None, "replace")
                model.hooks.clear()
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                os.chmod(path, 0o640)
                self.assert_unchanged(model, path, expected)

    def test_source_acl_change_before_rename_is_refused(self):
        for model in models():
            with self.scenario(model) as (path, expected):
                faults = Hooked({"replace.fsync": lambda: model.set_path(path, model.native, b"changed")})
                with self.assertRaisesRegex(Refused, "changed before"):
                    atomic_write(path, "new", faults, "replace")
                self.assertNotIn("replace.rename", faults.reached)
                self.assertEqual(path.read_text(), "old")
                self.assertEqual(model.get_path(path)[model.native], b"changed")
                self.assertFalse((path.parent / ".module.lua.new").exists())

    def test_denied_acl_write_is_refused_and_leaves_no_temporary_file(self):
        for model in models():
            with self.scenario(model) as (path, expected):
                for code in (errno.EACCES, errno.EPERM, errno.EOPNOTSUPP):
                    model.hooks["setxattr"] = on_temporary(raise_errno(code))
                    with self.assertRaisesRegex(Refused, "cannot preserve access metadata"):
                        atomic_write(path, "new", None, "replace")
                    model.hooks.clear()
                    self.assert_unchanged(model, path, expected)

    def test_written_acl_that_reads_back_differently_is_refused(self):
        for model in models():
            with self.scenario(model) as (path, expected):
                original = model.setxattr

                def lossy(fd, name, value, *args):
                    original(fd, name, bytes(value)[:-1], *args)

                model.setxattr = lossy
                with model.installed():
                    with self.assertRaisesRegex(Refused, "did not round-trip"):
                        atomic_write(path, "new", None, "replace")
                model.setxattr = original
                self.assert_unchanged(model, path, expected)

    def test_mode_recomputed_by_acl_restoration_is_refused(self):
        for model in models():
            with self.scenario(model) as (path, expected):
                real_chmod = os.chmod

                def recompute(fd, name):
                    real_chmod(fd_path(fd), 0o600)

                model.hooks["setxattr"] = on_temporary(recompute)
                with self.assertRaisesRegex(Refused, "ownership or mode did not round-trip"):
                    atomic_write(path, "new", None, "replace")
                model.hooks.clear()
                self.assert_unchanged(model, path, expected)

    def test_inherited_acl_is_removed_when_the_source_has_none(self):
        for model in models():
            with self.scenario(model) as (path, expected):
                plain = path.parent / "plain.lua"
                plain.write_text("old")
                plain.chmod(0o644)
                model.inherit[model.native] = b"inherited"
                atomic_write(plain, "new", None, "replace")
                self.assertEqual(plain.read_text(), "new")
                self.assertEqual(model.get_path(plain), {})
                self.assertEqual(stat.S_IMODE(plain.stat().st_mode), 0o644)
                self.assertIn(model.native, [name for _, name in model.ops("removexattr")])

    def test_denied_removal_of_an_inherited_acl_is_refused(self):
        for model in models():
            with self.scenario(model) as (path, expected):
                plain = path.parent / "plain.lua"
                plain.write_text("old")
                model.inherit[model.native] = b"inherited"
                model.hooks["removexattr"] = on_temporary(raise_errno(errno.EPERM))
                with self.assertRaisesRegex(Refused, "cannot preserve access metadata"):
                    atomic_write(plain, "new", None, "replace")
                self.assertEqual(plain.read_text(), "old")
                self.assertEqual(model.get_path(plain), {})
                self.assertFalse((path.parent / ".plain.lua.new").exists())

    def test_symlink_is_still_refused_before_inspection(self):
        for model in models():
            with self.scenario(model) as (path, expected):
                link = path.parent / "link.lua"
                link.symlink_to(path)
                with self.assertRaisesRegex(Refused, "symlink"):
                    atomic_write(link, "new", None, "replace")
                self.assertTrue(link.is_symlink())
                self.assertEqual(model.calls, [])
                self.assert_unchanged(model, path, expected)

    def test_symlinked_metadata_source_is_refused(self):
        for model in models():
            with self.scenario(model) as (path, expected):
                link = path.parent / "policy.lua"
                link.symlink_to(path)
                target = path.parent / "target.lua"
                with self.assertRaisesRegex(Refused, "cannot read access metadata"):
                    atomic_write(target, "new", None, "replace", metadata_from=link)
                self.assertFalse(target.exists())
                self.assert_unchanged(model, path, expected)

if __name__ == "__main__":
    unittest.main()

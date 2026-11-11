"""Unlisted NFSv4 probes require a listed, readable POSIX access ACL."""

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import stat
import tempfile
import unittest

from eb_staged_rebuild.atomicio import atomic_write
from eb_staged_rebuild.errors import Refused
from tests.acl_fakes import AclModel, GPFS, NFS4, OPAQUE, POSIX, models, raise_errno
from tests.test_acl_models import only


class AclProbeCompatibilityTests(unittest.TestCase):
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

    def test_unlisted_einval_acl_probe_is_treated_as_not_provided(self):
        """Compatibility: unlisted NFSv4/GPFS names may return EINVAL.

        POSIX remains listed and readable; probing the other known ACL names
        with EINVAL must not refuse replacement. Listed-path fail-closed
        behaviour is covered separately.
        """
        model = AclModel(
            "posix-access-einval-foreign",
            POSIX,
            {POSIX},
            chmod_rewrites=False,
        )
        with tempfile.TemporaryDirectory() as directory, model.installed():
            path = Path(directory) / "module.lua"
            path.write_text("old")
            path.chmod(0o640)
            model.set_path(path, POSIX, OPAQUE)
            expected = model.get_path(path)
            listed = set(expected)

            def probe(fd, name):
                if name not in listed:
                    raise OSError(errno.EINVAL, os.strerror(errno.EINVAL))
                return None

            model.hooks["getxattr"] = only(path, probe)
            atomic_write(path, "new", None, "replace")
            model.hooks.clear()
            self.assertEqual(path.read_text(), "new")
            self.assertEqual(model.get_path(path), expected)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_listed_einval_acl_read_is_refused_not_treated_as_absent(self):
        for model in models():
            with self.scenario(model) as (path, expected):
                model.hooks["getxattr"] = only(path, raise_errno(errno.EINVAL))
                with self.assertRaises(Refused):
                    atomic_write(path, "new", None, "replace")
                model.hooks.clear()
                self.assert_unchanged(model, path, expected)

    def test_unlisted_einval_does_not_weaken_eacces_or_eperm_probes(self):
        for model in models():
            with self.scenario(model) as (path, expected):
                listed = set(expected)
                if listed == {GPFS, NFS4, POSIX}:
                    continue
                for code in (errno.EACCES, errno.EPERM):
                    def probe(fd, name, code=code):
                        if name not in listed:
                            raise OSError(code, os.strerror(code))
                        return None

                    model.hooks["getxattr"] = only(path, probe)
                    with self.assertRaises(Refused):
                        atomic_write(path, "new", None, "replace")
                    model.hooks.clear()
                    self.assert_unchanged(model, path, expected)

    def test_unlisted_nfs4_family_einval_with_listed_posix_retains_posix_bytes(self):
        """Narrow guard: unlisted gpfs/nfs4 EINVAL only with listed readable POSIX."""
        posix_bytes = OPAQUE + b"|posix-retained"
        model = AclModel("posix-only", POSIX, {POSIX}, chmod_rewrites=False)
        with tempfile.TemporaryDirectory() as directory, model.installed():
            path = Path(directory) / "module.lua"
            path.write_text("old")
            path.chmod(0o640)
            model.set_path(path, POSIX, posix_bytes)
            expected = model.get_path(path)
            self.assertEqual(expected, {POSIX: posix_bytes})

            def probe(fd, name):
                if name in (GPFS, NFS4):
                    raise OSError(errno.EINVAL, os.strerror(errno.EINVAL))
                return None

            model.hooks["getxattr"] = only(path, probe)
            atomic_write(path, "new", None, "replace")
            model.hooks.clear()
            self.assertEqual(path.read_text(), "new")
            self.assertEqual(model.get_path(path), {POSIX: posix_bytes})
            self.assertEqual(model.get_path(path)[POSIX], posix_bytes)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_unlisted_posix_einval_without_positive_model_is_refused(self):
        model = AclModel("nfs4-only", NFS4, {NFS4})
        with self.scenario(model) as (path, expected):
            def probe(fd, name):
                if name == POSIX:
                    raise OSError(errno.EINVAL, os.strerror(errno.EINVAL))
                return None

            model.hooks["getxattr"] = only(path, probe)
            with self.assertRaises(Refused):
                atomic_write(path, "new", None, "replace")
            model.hooks.clear()
            self.assert_unchanged(model, path, expected)

    def test_unlisted_nfs4_einval_without_listed_posix_is_refused(self):
        model = AclModel("nfs4-only-no-posix", NFS4, {NFS4})
        with self.scenario(model) as (path, expected):
            def probe(fd, name):
                if name in (GPFS, POSIX):
                    raise OSError(errno.EINVAL, os.strerror(errno.EINVAL))
                return None

            model.hooks["getxattr"] = only(path, probe)
            with self.assertRaises(Refused):
                atomic_write(path, "new", None, "replace")
            model.hooks.clear()
            self.assert_unchanged(model, path, expected)

    def test_listed_nfs4_einval_is_refused(self):
        model = AclModel("nfs4-generic-listed", NFS4, {NFS4})
        with self.scenario(model) as (path, expected):
            model.hooks["getxattr"] = only(path, raise_errno(errno.EINVAL))
            with self.assertRaises(Refused):
                atomic_write(path, "new", None, "replace")
            model.hooks.clear()
            self.assert_unchanged(model, path, expected)

    def test_listed_gpfs_einval_is_refused(self):
        model = AclModel("gpfs-native-listed", GPFS, {GPFS})
        with self.scenario(model) as (path, expected):
            model.hooks["getxattr"] = only(path, raise_errno(errno.EINVAL))
            with self.assertRaises(Refused):
                atomic_write(path, "new", None, "replace")
            model.hooks.clear()
            self.assert_unchanged(model, path, expected)

    def test_narrow_einval_guard_still_refuses_denied_nfs4_probe(self):
        model = AclModel("posix-only-denied-nfs4", POSIX, {POSIX}, chmod_rewrites=False)
        with tempfile.TemporaryDirectory() as directory, model.installed():
            path = Path(directory) / "module.lua"
            path.write_text("old")
            path.chmod(0o640)
            model.set_path(path, POSIX, OPAQUE)
            expected = model.get_path(path)
            for code in (errno.EACCES, errno.EPERM):
                def probe(fd, name, code=code):
                    if name in (GPFS, NFS4):
                        raise OSError(code, os.strerror(code))
                    return None

                model.hooks["getxattr"] = only(path, probe)
                with self.assertRaises(Refused):
                    atomic_write(path, "new", None, "replace")
                model.hooks.clear()
                self.assert_unchanged(model, path, expected)

    def test_narrow_einval_guard_still_refuses_changing_metadata(self):
        model = AclModel("posix-only-changing", POSIX, {POSIX}, chmod_rewrites=False)
        with tempfile.TemporaryDirectory() as directory, model.installed():
            path = Path(directory) / "module.lua"
            path.write_text("old")
            path.chmod(0o640)
            model.set_path(path, POSIX, OPAQUE)
            expected = model.get_path(path)
            lists = []

            def changing_list(fd, name):
                lists.append(fd)
                if len(lists) == 2:
                    model.set_path(path, POSIX, b"other-posix")
                return None

            def einval_nfs4(fd, name):
                if name in (GPFS, NFS4):
                    raise OSError(errno.EINVAL, os.strerror(errno.EINVAL))
                return None

            model.hooks["listxattr"] = only(path, changing_list)
            model.hooks["getxattr"] = only(path, einval_nfs4)
            with self.assertRaisesRegex(Refused, "changed during inspection"):
                atomic_write(path, "new", None, "replace")
            model.hooks.clear()
            model.set_path(path, POSIX, OPAQUE)
            self.assert_unchanged(model, path, expected)


if __name__ == "__main__":
    unittest.main()

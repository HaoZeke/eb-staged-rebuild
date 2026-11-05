"""Exercise native NFSv4 metadata replacement on the supplied filesystem."""

import argparse
import errno
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from eb_staged_rebuild.atomicio import atomic_write
from eb_staged_rebuild.errors import Refused


options = None


def snapshot(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        return (os.getxattr(fd, 'system.nfs4_acl'), info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))
    finally:
        os.close(fd)


class NativeAclRoundTrip(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=options.workspace)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def file(self, name, mode=0o640):
        path = self.root / name
        path.write_text('old')
        path.chmod(mode)
        return path

    def test_virtual_acl_survives_replacement(self):
        path = self.file('module.lua')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            self.assertNotIn('system.nfs4_acl', os.listxattr(fd))
        finally:
            os.close(fd)
        expected = snapshot(path)
        atomic_write(path, 'new', None, 'replace')
        self.assertEqual(path.read_text(), 'new')
        self.assertEqual(snapshot(path), expected)

    def test_prepared_acl_and_rollback_are_exact(self):
        live = self.file('live.lua')
        prepared = self.file('prepared.lua', 0o600)
        backup = self.root / 'backup.lua'
        original = snapshot(live)
        policy = snapshot(prepared)
        self.assertNotEqual(original, policy)
        atomic_write(backup, 'old', None, 'backup', metadata_from=live)
        atomic_write(live, 'new', None, 'publish', metadata_from=prepared)
        self.assertEqual(snapshot(live), policy)
        atomic_write(live, 'old', None, 'rollback', metadata_from=backup)
        self.assertEqual(live.read_text(), 'old')
        self.assertEqual(snapshot(live), original)

    def test_read_only_metadata_source_is_preserved(self):
        source = Path(options.readonly_source)
        self.assertTrue(os.statvfs(source).f_flag & os.ST_RDONLY)
        expected = snapshot(source)
        destination = self.root / 'module.lua'
        atomic_write(destination, 'new', None, 'replace', metadata_from=source)
        self.assertEqual(destination.read_text(), 'new')
        self.assertEqual(snapshot(destination), expected)

    def test_denied_acl_probe_leaves_destination_intact(self):
        path = self.file('module.lua')
        expected = snapshot(path)
        with patch('eb_staged_rebuild.atomicio.os.getxattr', side_effect=OSError(errno.EACCES, 'denied')):
            with self.assertRaises(Refused):
                atomic_write(path, 'new', None, 'replace')
        self.assertEqual(path.read_text(), 'old')
        self.assertEqual(snapshot(path), expected)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--readonly-source', required=True)
    options = parser.parse_args()
    unittest.main(argv=['native_acl_roundtrip'], verbosity=2)

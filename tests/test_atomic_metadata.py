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
from eb_staged_rebuild.faults import FaultInjector, InjectedCrash


def access_acl():
    entries = [(0x01, 6, 0xffffffff), (0x02, 4, 65534),
               (0x04, 4, 0xffffffff), (0x10, 4, 0xffffffff),
               (0x20, 0, 0xffffffff)]
    return struct.pack('<I', 2) + b''.join(struct.pack('<HHI', *entry) for entry in entries)


class AtomicMetadataTests(unittest.TestCase):
    def test_replacement_preserves_owner_group_mode_and_access_acl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'module.lua'
            path.write_text('old')
            os.setxattr(path, 'system.posix_acl_access', access_acl())
            before = path.stat()
            acl = os.getxattr(path, 'system.posix_acl_access')
            atomic_write(path, 'new', None, 'replace')
            after = path.stat()
            self.assertEqual(path.read_text(), 'new')
            self.assertEqual((after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)),
                             (before.st_uid, before.st_gid, stat.S_IMODE(before.st_mode)))
            self.assertEqual(os.getxattr(path, 'system.posix_acl_access'), acl)

    def test_replacement_does_not_gain_the_parent_default_acl(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'module.lua'
            path.write_text('old')
            os.setxattr(root, 'system.posix_acl_default', access_acl())
            atomic_write(path, 'new', None, 'replace')
            with self.assertRaises(OSError) as failure:
                os.getxattr(path, 'system.posix_acl_access')
            self.assertEqual(failure.exception.errno, errno.ENODATA)
            self.assertEqual(path.read_text(), 'new')

    def test_acl_copy_failure_preserves_the_live_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'module.lua'
            path.write_text('old')
            os.setxattr(path, 'system.posix_acl_access', access_acl())
            acl = os.getxattr(path, 'system.posix_acl_access')
            with patch('eb_staged_rebuild.atomicio.os.setxattr', side_effect=PermissionError('ACL denied')):
                with self.assertRaises(Refused):
                    atomic_write(path, 'new', None, 'replace')
            self.assertEqual(path.read_text(), 'old')
            self.assertEqual(os.getxattr(path, 'system.posix_acl_access'), acl)

    def test_ownership_copy_failure_preserves_the_live_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'module.lua'
            path.write_text('old')
            with patch('eb_staged_rebuild.atomicio.os.fchown', side_effect=PermissionError('group denied')):
                with self.assertRaises(Refused):
                    atomic_write(path, 'new', None, 'replace')
            self.assertEqual(path.read_text(), 'old')

    def test_resume_replaces_an_owned_readonly_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'module.lua'
            path.write_text('old')
            path.chmod(0o444)
            with self.assertRaises(InjectedCrash):
                atomic_write(path, 'partial', FaultInjector('replace.rename'), 'replace')
            self.assertEqual(path.read_text(), 'old')
            atomic_write(path, 'new', None, 'replace')
            self.assertEqual(path.read_text(), 'new')
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o444)

    def test_hardlinked_temporary_file_cannot_modify_another_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'module.lua'
            outside = root / 'outside'
            path.write_text('old')
            outside.write_text('protected')
            os.link(outside, root / '.module.lua.new')
            with self.assertRaises(Refused):
                atomic_write(path, 'new', None, 'replace')
            self.assertEqual(path.read_text(), 'old')
            self.assertEqual(outside.read_text(), 'protected')


if __name__ == '__main__':
    unittest.main()

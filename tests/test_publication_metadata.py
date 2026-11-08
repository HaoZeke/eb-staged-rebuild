import errno
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from eb_staged_rebuild.engine import Spec, rollback, run
from tests.support import PROBES, classic_install, installpath_builder
from tests.test_atomic_metadata import access_acl


class PublicationMetadataTests(unittest.TestCase):
    def fixture(self, directory, existing=False, policy=False):
        root = Path(directory)
        software, modules, state = (root / name for name in ('software', 'modules', 'state'))
        software.mkdir()
        modules.mkdir()
        module = modules / 'all/demo/1.lua'
        if existing:
            classic_install(str(software), str(modules), 'demo/1', 'old', 'all/demo/1.lua')
            module.chmod(0o600)
        build = installpath_builder([{'rel': 'demo/1', 'payload': 'new',
                                      'module_rel': 'all/demo/1.lua'}])

        def builder(ctx):
            code = build(ctx)
            staged = Path(ctx.generation_root) / 'modules/all/demo/1.lua'
            os.setxattr(staged, 'system.posix_acl_access', access_acl())
            return code

        executable = None
        if policy:
            executable = root / 'policy'
            executable.write_text(
                '#!/usr/bin/env python3\nimport json, os, sys\nfrom pathlib import Path\n'
                'context = json.loads(Path(sys.argv[1]).read_text())\n'
                'for module in context["modules"]:\n'
                '    os.setxattr(module["prepared"], "system.posix_acl_access", '
                + repr(access_acl()) + ')\n'
            )
            executable.chmod(0o755)
        spec = Spec(backend="installpath", software=str(software), modules=str(modules), state_root=str(state),
                    eb_args=['demo.eb'], module_rels=['demo/1'], run_id='metadata',
                    probes=PROBES, builder=builder,
                    prepare_executable=str(executable) if executable else None)
        return spec, module

    def test_new_module_preserves_staged_acl_with_restrictive_umask(self):
        with tempfile.TemporaryDirectory() as directory:
            spec, module = self.fixture(directory)
            previous = os.umask(0o077)
            try:
                result = run(spec)
            finally:
                os.umask(previous)
            self.assertEqual(result.returncode, 0, result.message)
            self.assertEqual(os.getxattr(module, 'system.posix_acl_access'), access_acl())
            self.assertEqual(stat.S_IMODE(module.stat().st_mode), 0o640)

    def test_finalizer_metadata_is_applied_at_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            spec, module = self.fixture(directory, existing=True, policy=True)
            result = run(spec)
            self.assertEqual(result.returncode, 0, result.message)
            self.assertEqual(os.getxattr(module, 'system.posix_acl_access'), access_acl())
            self.assertEqual(stat.S_IMODE(module.stat().st_mode), 0o640)
            context = json.loads((Path(spec.state_root) / 'runs/metadata/prepare-context.json').read_text())
            self.assertEqual(len(context['modules']), 1)
            self.assertTrue(Path(context['modules'][0]['prepared']).is_file())

    def test_rollback_restores_previous_mode_and_absence_of_acl(self):
        with tempfile.TemporaryDirectory() as directory:
            spec, module = self.fixture(directory, existing=True, policy=True)
            previous = module.read_bytes()
            result = run(spec)
            self.assertEqual(result.returncode, 0, result.message)
            restored = rollback(spec.state_root, 'metadata')
            self.assertEqual(restored.returncode, 0, restored.message)
            self.assertEqual(module.read_bytes(), previous)
            self.assertEqual(stat.S_IMODE(module.stat().st_mode), 0o600)
            with self.assertRaises(OSError) as failure:
                os.getxattr(module, 'system.posix_acl_access')
            self.assertEqual(failure.exception.errno, errno.ENODATA)


if __name__ == '__main__':
    unittest.main()

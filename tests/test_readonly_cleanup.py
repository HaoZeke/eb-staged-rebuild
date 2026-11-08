import os
from pathlib import Path
import tempfile
import unittest

from eb_staged_rebuild.engine import Spec, resume, run
from eb_staged_rebuild.faults import FaultInjector, InjectedCrash
from tests.support import PROBES, classic_install, installpath_builder


def seal_tree(path):
    for directory, dirs, files in os.walk(path, followlinks=False):
        for filename in files:
            target = Path(directory) / filename
            if not target.is_symlink():
                target.chmod(0o444)
        for name in dirs:
            target = Path(directory) / name
            if not target.is_symlink():
                target.chmod(0o555)
    Path(path).chmod(0o555)


class ReadonlyCleanupTests(unittest.TestCase):
    def fixture(self, directory):
        root = Path(directory)
        software, modules, state = (root / name for name in ('software', 'modules', 'state'))
        software.mkdir()
        modules.mkdir()
        original, module, text = classic_install(str(software), str(modules), 'demo/1', 'old\n', 'all/demo/1.lua')
        spec = Spec(backend="installpath", software=str(software), modules=str(modules), state_root=str(state),
                    eb_args=['demo.eb'], module_rels=['demo/1'], run_id='sealed', probes=PROBES,
                    clock=lambda: 100, builder=installpath_builder([
                        {'rel': 'demo/1', 'payload': 'new\n', 'module_rel': 'all/demo/1.lua'}]))
        return spec, Path(original), Path(module), text

    def test_resume_can_replace_a_readonly_module_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            spec, original, module, text = self.fixture(directory)
            builder = spec.builder

            def sealed(ctx):
                code = builder(ctx)
                seal_tree(Path(ctx.generation_root) / 'modules')
                return code

            spec.builder = sealed
            spec.faults = FaultInjector('build.finish')
            with self.assertRaises(InjectedCrash):
                run(spec)
            self.assertEqual(module.read_text(), text)
            result = resume(spec.state_root, 'sealed', builder=sealed)
            self.assertEqual(result.returncode, 0, result.message)
            # The swap published the rebuilt tree at the stable path and kept
            # the previous one beside it.
            self.assertEqual((original / 'PAYLOAD').read_text(), 'new\n')
            self.assertEqual((original.parent / '.1.sealed' / 'PAYLOAD').read_text(), 'old\n')


if __name__ == '__main__':
    unittest.main()

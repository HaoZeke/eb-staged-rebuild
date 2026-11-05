from pathlib import Path
import tempfile
import unittest

from eb_staged_rebuild.engine import Spec, run
from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.gc import collect
from tests.support import PROBES, classic_install, installpath_builder


class GarbageCollectionPathResolutionTests(unittest.TestCase):
    def fixture(self, directory):
        root = Path(directory)
        software, modules, state = (root / name for name in ('software', 'modules', 'state'))
        software.mkdir()
        modules.mkdir()
        _, module, _ = classic_install(str(software), str(modules), 'demo/1', 'old', 'all/demo/1.lua')
        spec = Spec(software=str(software), modules=str(modules), state_root=str(state),
                    eb_args=['demo.eb'], module_rels=['demo/1'], run_id='first',
                    probes=PROBES, clock=lambda: 100,
                    builder=installpath_builder([{'rel': 'demo/1', 'payload': 'first',
                                                 'module_rel': 'all/demo/1.lua'}]))
        self.assertEqual(run(spec).returncode, 0)
        first_module = Path(module).read_text()
        spec.run_id = 'second'
        spec.clock = lambda: 200
        self.assertEqual(run(spec).returncode, 0)
        Path(module).write_text(first_module)
        return root, Path(module), state

    def test_unsearchable_module_parent_refuses_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            _, module, state = self.fixture(directory)
            parent = module.parent.parent
            parent.chmod(0)
            try:
                with self.assertRaises(Refused):
                    collect(str(state), grace_hours=0, now=10000)
                self.assertTrue((state / 'generations/first/software/demo/1/PAYLOAD').is_file())
                self.assertTrue((state / 'generations/second/software/demo/1/PAYLOAD').is_file())
            finally:
                parent.chmod(0o700)

    def test_symlinked_root_keeps_the_referenced_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root, module, state = self.fixture(directory)
            alias = root / 'alias'
            alias.symlink_to(root, target_is_directory=True)
            module.write_text(module.read_text().replace(str(state), str(alias / 'state')))
            actions = collect(str(state), grace_hours=0, now=10000)
            first = state / 'generations/first'
            self.assertTrue((first / 'software/demo/1/PAYLOAD').is_file())
            self.assertTrue(any(action['path'] == str(first) and action['action'] == 'keep'
                                for action in actions))

    def test_unresolvable_live_root_refuses_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root, module, state = self.fixture(directory)
            alias = root / 'alias'
            alias.symlink_to(root / 'missing', target_is_directory=True)
            module.write_text(module.read_text().replace(str(state), str(alias / 'state')))
            with self.assertRaises(Refused):
                collect(str(state), grace_hours=0, now=10000)
            self.assertTrue((state / 'generations/first/software/demo/1/PAYLOAD').is_file())


if __name__ == '__main__':
    unittest.main()

from pathlib import Path
import tempfile
import unittest

from eb_staged_rebuild.engine import Spec, run
from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.gc import collect
from tests.support import PROBES, classic_install, installpath_builder


class GarbageCollectionReferenceTests(unittest.TestCase):
    def check_reference_failure(self, corruption):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            software, modules, state = (root / name for name in ('software', 'modules', 'state'))
            software.mkdir()
            modules.mkdir()
            _, module, _ = classic_install(str(software), str(modules), 'demo/1', 'old', 'all/demo/1.lua')
            spec = Spec(backend="installpath", software=str(software), modules=str(modules), state_root=str(state),
                        eb_args=['demo.eb'], module_rels=['demo/1'], run_id='first',
                        probes=PROBES, clock=lambda: 100,
                        builder=installpath_builder([{'rel': 'demo/1', 'payload': 'first',
                                                     'module_rel': 'all/demo/1.lua'}]))
            self.assertEqual(run(spec).returncode, 0)
            first_module = Path(module).read_bytes()
            spec.run_id = 'second'
            spec.clock = lambda: 200
            self.assertEqual(run(spec).returncode, 0)
            Path(module).write_bytes(first_module)
            corruption(Path(module))
            try:
                with self.assertRaises(Refused):
                    collect(str(state), grace_hours=0, now=10000)
                self.assertTrue((state / 'generations/first/software/demo/1/PAYLOAD').is_file())
                self.assertTrue((state / 'generations/second/software/demo/1/PAYLOAD').is_file())
            finally:
                Path(module).chmod(0o600)

    def test_unreadable_live_module_prevents_generation_removal(self):
        self.check_reference_failure(lambda module: module.chmod(0o000))

    def test_invalid_utf8_live_module_prevents_generation_removal(self):
        self.check_reference_failure(lambda module: module.write_bytes(b'\xff'))

    def test_unrecognized_live_root_prevents_generation_removal(self):
        self.check_reference_failure(lambda module: module.write_text('-- root cannot be determined\n'))


if __name__ == '__main__':
    unittest.main()

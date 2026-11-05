from pathlib import Path
import tempfile
import unittest

from eb_staged_rebuild.engine import Spec, run
from eb_staged_rebuild.errors import Refused
from tests.support import PROBES, classic_install, installpath_builder


class PrepareBarrierTests(unittest.TestCase):
    def fixture(self, directory, operation):
        root = Path(directory)
        software, modules, state = (root / name for name in ('software', 'modules', 'state'))
        software.mkdir()
        modules.mkdir()
        originals = []
        packages = []
        for name in ['alpha', 'beta']:
            _, module, text = classic_install(str(software), str(modules), name + '/1',
                                             'old\n', 'all/' + name + '/1.lua', name=name)
            originals.append((Path(module), text))
            packages.append({'rel': name + '/1', 'name': name, 'payload': 'new\n',
                             'module_rel': 'all/' + name + '/1.lua'})
        policy = root / 'policy'
        policy.write_text(
            '#!/usr/bin/env python3\nimport json, sys\nfrom pathlib import Path\n'
            'context = json.loads(Path(sys.argv[1]).read_text())\n'
            'package = next(Path(m["generation"]) for m in context["modules"] '
            'if Path(m["generation"]).parent.name == "beta")\n'
            '(package / "PAYLOAD").unlink()\n' + operation + '\n'
        )
        policy.chmod(0o755)
        spec = Spec(software=str(software), modules=str(modules), state_root=str(state),
                    eb_args=['example.eb'], module_rels=['alpha/1', 'beta/1'], run_id='barrier',
                    probes=PROBES, prepare_executable=str(policy), builder=installpath_builder(packages))
        return spec, originals

    def assert_refused_without_publication(self, spec, originals):
        try:
            result = run(spec)
        except Refused:
            pass
        else:
            self.assertEqual(result.returncode, 2, result.message)
        for module, text in originals:
            self.assertEqual(module.read_text(), text)

    def test_incomplete_second_generation_prevents_first_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            spec, originals = self.fixture(directory, '(package / "easybuild").rename(package / "disabled-metadata")')
            self.assert_refused_without_publication(spec, originals)

    def test_unreadable_second_generation_prevents_first_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            spec, originals = self.fixture(directory, '(package / "easybuild").chmod(0o000)')
            metadata = Path(spec.state_root) / 'generations/barrier/software/beta/1/easybuild'
            try:
                self.assert_refused_without_publication(spec, originals)
            finally:
                if metadata.exists():
                    metadata.chmod(0o700)


if __name__ == '__main__':
    unittest.main()

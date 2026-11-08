import json
from pathlib import Path
import stat
import tempfile
import unittest

from eb_staged_rebuild.engine import Spec, resume, run
from eb_staged_rebuild.faults import FaultInjector, InjectedCrash
from eb_staged_rebuild.journal import journal_path, load_journal
from eb_staged_rebuild.modulefile import parse_root
from tests.support import PROBES, classic_install, installpath_builder


class PreparePublicationTests(unittest.TestCase):
    def fixture(self, directory, returncode=0):
        root = Path(directory)
        software, modules, state = (root / name for name in ('software', 'modules', 'state'))
        software.mkdir()
        modules.mkdir()
        original, module, text = classic_install(str(software), str(modules), 'demo/1', 'old\n', 'all/demo/1.lua')
        policy = root / 'finalize'
        count = root / 'prepare-count'
        policy.write_text(
            '#!/usr/bin/env python3\n'
            'import json, sys\nfrom pathlib import Path\n'
            'context = json.loads(Path(sys.argv[1]).read_text())\n'
            f'assert context["run_id"] == "prepare"\nassert Path({module!r}).read_text() == {text!r}\n'
            'assert len(context["modules"]) == 1\n'
            'package = Path(context["modules"][0]["generation"])\n'
            'assert package.is_relative_to(context["stage_software"])\n'
            '(package / "easybuild").chmod(0o700)\n'
            f'with Path({str(count)!r}).open("a") as stream: stream.write("called\\n")\n'
            f'sys.exit({returncode})\n'
        )
        policy.chmod(0o755)
        spec = Spec(backend="installpath", 
            software=str(software), modules=str(modules), state_root=str(state),
            eb_args=['demo.eb'], module_rels=['demo/1'], run_id='prepare',
            probes=PROBES, prepare_executable=str(policy),
            builder=installpath_builder([{'rel': 'demo/1', 'payload': 'new\n', 'module_rel': 'all/demo/1.lua'}]),
        )
        return spec, Path(module), text, count

    def test_finalizer_restricts_generation_before_live_module_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            spec, module, text, count = self.fixture(directory)
            result = run(spec)
            self.assertEqual(result.returncode, 0, result.message)
            # The finalizer restricted the staged tree; the swap published
            # that tree at the stable path the module names.
            live = Path(spec.software) / 'demo/1'
            self.assertEqual(parse_root(module.read_text())[1], str(live))
            self.assertEqual(stat.S_IMODE((live / 'easybuild').stat().st_mode), 0o700)
            self.assertEqual(count.read_text(), 'called\n')
            self.assertEqual((live / 'PAYLOAD').read_text(), 'new\n')

    def test_finalizer_nonzero_exit_preserves_live_module(self):
        with tempfile.TemporaryDirectory() as directory:
            spec, module, text, count = self.fixture(directory, returncode=42)
            result = run(spec)
            self.assertEqual(result.returncode, 42)
            self.assertEqual(module.read_text(), text)
            self.assertEqual((Path(spec.software) / 'demo/1/PAYLOAD').read_text(), 'old\n')
            journal = load_journal(journal_path(spec.state_root, 'prepare'))
            self.assertFalse(journal['prepare_complete'])
            self.assertEqual(journal['prepare_returncode'], 42)
            self.assertTrue(all(m['status'] == 'pending' for m in journal['modules_plan']))

    def test_missing_finalizer_preserves_live_module(self):
        with tempfile.TemporaryDirectory() as directory:
            spec, module, text, count = self.fixture(directory)
            spec.prepare_executable = str(Path(directory) / 'missing')
            result = run(spec)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(module.read_text(), text)
            self.assertFalse(count.exists())

    def test_resume_from_built_runs_unfinished_finalizer(self):
        with tempfile.TemporaryDirectory() as directory:
            spec, module, text, count = self.fixture(directory)
            spec.faults = FaultInjector('prepare.invoke')
            with self.assertRaises(InjectedCrash):
                run(spec)
            self.assertEqual(module.read_text(), text)
            self.assertFalse(count.exists())
            result = resume(spec.state_root, 'prepare', builder=spec.builder)
            self.assertEqual(result.returncode, 0, result.message)
            self.assertEqual(count.read_text(), 'called\n')
            self.assertEqual((Path(spec.software) / 'demo/1/PAYLOAD').read_text(), 'new\n')

    def test_resume_during_publication_does_not_repeat_finalizer(self):
        with tempfile.TemporaryDirectory() as directory:
            spec, module, text, count = self.fixture(directory)
            spec.faults = FaultInjector('journal.publishing.write')
            with self.assertRaises(InjectedCrash):
                run(spec)
            self.assertEqual(module.read_text(), text)
            self.assertEqual((Path(spec.software) / 'demo/1/PAYLOAD').read_text(), 'old\n')
            self.assertEqual(count.read_text(), 'called\n')
            result = resume(spec.state_root, 'prepare', builder=spec.builder)
            self.assertEqual(result.returncode, 0, result.message)
            self.assertEqual(count.read_text(), 'called\n')
            self.assertEqual((Path(spec.software) / 'demo/1/PAYLOAD').read_text(), 'new\n')


if __name__ == '__main__':
    unittest.main()

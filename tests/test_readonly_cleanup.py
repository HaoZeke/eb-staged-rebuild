import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from eb_staged_rebuild.engine import Spec, resume, rollback, run
from eb_staged_rebuild.faults import FaultInjector, InjectedCrash
from eb_staged_rebuild.gc import collect
from eb_staged_rebuild.lockfile import FileLock
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
        spec = Spec(software=str(software), modules=str(modules), state_root=str(state),
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
            self.assertEqual((original / 'PAYLOAD').read_text(), 'old\n')
            self.assertNotEqual(module.read_text(), text)

    def test_gc_removes_a_sealed_generation_without_following_links(self):
        with tempfile.TemporaryDirectory() as directory:
            spec, original, _, _ = self.fixture(directory)
            self.assertEqual(run(spec).returncode, 0)
            self.assertEqual(rollback(spec.state_root, 'sealed', clock=lambda: 101).returncode, 0)
            generation = Path(spec.state_root) / 'generations/sealed'
            (generation / 'outside').symlink_to(original, target_is_directory=True)
            original_mode = original.stat().st_mode
            seal_tree(generation)
            actions = collect(spec.state_root, grace_hours=0, now=102)
            self.assertTrue(any(a['action'] == 'remove' for a in actions))
            self.assertFalse(generation.exists())
            self.assertEqual((original / 'PAYLOAD').read_text(), 'old\n')
            self.assertEqual(original.stat().st_mode, original_mode)

    def test_gc_waits_for_the_publication_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            spec, original, _, _ = self.fixture(directory)
            self.assertEqual(run(spec).returncode, 0)
            self.assertEqual(rollback(spec.state_root, 'sealed', clock=lambda: 101).returncode, 0)
            generation = Path(spec.state_root) / 'generations/sealed'
            holder = FileLock(spec.state_root)
            holder.acquire()
            program = ('from eb_staged_rebuild.gc import collect; '
                       'print("READY", flush=True); '
                       f'collect({spec.state_root!r}, grace_hours=0, now=102)')
            process = subprocess.Popen([sys.executable, '-c', program], stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(process.stdout.readline().strip(), 'READY')
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.communicate(timeout=0.3)
                self.assertTrue(generation.exists())
            finally:
                holder.release()
                stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertFalse(generation.exists())
            self.assertEqual((original / 'PAYLOAD').read_text(), 'old\n')


if __name__ == '__main__':
    unittest.main()

import contextlib
import io
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch

from eb_staged_rebuild import cli
from eb_staged_rebuild.builders import run_command
from eb_staged_rebuild.engine import resume, run
from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.faults import FaultInjector
from eb_staged_rebuild.journal import journal_path, load_journal
from tests import test_prepare_publication as preparation


class PrepareRecoveryTests(unittest.TestCase):
    fixture = preparation.PreparePublicationTests.fixture

    def test_empty_program_is_refused_by_the_api(self):
        with tempfile.TemporaryDirectory() as directory:
            spec, module, text, _ = self.fixture(directory)
            spec.prepare_executable = ''
            with self.assertRaisesRegex(Refused, 'empty'):
                run(spec)
            self.assertEqual(module.read_text(), text)

    def test_empty_program_is_refused_before_cli_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            spec, _, _, _ = self.fixture(directory)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), patch.object(cli, 'run') as invoke:
                code = cli.main(['run', '--software', spec.software, '--modules', spec.modules,
                                 '--prepare', '', '--dry-run', '--', 'demo.eb'])
            self.assertEqual(code, 2)
            self.assertIn('empty', stderr.getvalue())
            invoke.assert_not_called()

    def test_failed_policy_retry_preserves_the_built_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            spec, module, text, _ = self.fixture(directory, returncode=42)
            builder = spec.builder
            calls = []

            def counted(ctx):
                calls.append(ctx.generation_root)
                return builder(ctx)

            spec.builder = counted
            result = run(spec)
            self.assertEqual(result.returncode, 42)
            self.assertEqual(module.read_text(), text)
            journal = load_journal(journal_path(spec.state_root, 'prepare'))
            self.assertEqual(journal['state'], 'built')
            policy = Path(spec.prepare_executable)
            policy.write_text(policy.read_text().replace('sys.exit(42)', 'sys.exit(0)'))
            result = resume(spec.state_root, 'prepare', builder=counted)
            self.assertEqual(result.returncode, 0, result.message)
            self.assertEqual(len(calls), 1)
            self.assertEqual((Path(spec.software) / 'demo/1/PAYLOAD').read_text(), 'new\n')

    def test_child_signal_has_a_shell_exit_status(self):
        command = [sys.executable, '-c', 'import os, signal; os.kill(os.getpid(), signal.SIGTERM)']
        import os
        code = run_command(command, os.environ.copy(), FaultInjector(), 'child.signal')
        self.assertEqual(code, 128 + signal.SIGTERM)

    def test_policy_journal_requires_schema_two(self):
        with tempfile.TemporaryDirectory() as directory:
            spec, _, _, _ = self.fixture(directory)
            self.assertEqual(run(spec).returncode, 0)
            journal = load_journal(journal_path(spec.state_root, 'prepare'))
            self.assertEqual(journal['schema'], 2)


if __name__ == '__main__':
    unittest.main()

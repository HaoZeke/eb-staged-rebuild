"""A recorded publication retains its ledger policy across restart."""

import json
from pathlib import Path
import tempfile
import unittest

from eb_staged_rebuild.engine import run, resume
from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.journal import load_journal
from tests.test_ledger_outbox import Fixture


class LedgerPolicyTests(unittest.TestCase):
    def test_ledger_run_uses_a_schema_that_older_publishers_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            result = run(fixture.spec())
            self.assertEqual(result.returncode, 0, result.message)
            journal = load_journal(Path(fixture.state) / 'runs/run1/journal.json')
            self.assertEqual(journal['schema'], 3)
            self.assertEqual(journal['ledger_policy']['required'], True)
            self.assertEqual(journal['ledger_policy']['log'], fixture.log)

    def test_required_policy_cannot_change_while_a_record_is_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            fixture.plan_modes('fail-before')
            self.assertEqual(run(fixture.spec()).returncode, 75)
            config = Path(fixture.config)
            policy = json.loads(config.read_text())
            policy['required'] = False
            config.write_text(json.dumps(policy))
            with self.assertRaisesRegex(Refused, 'ledger policy changed'):
                resume(fixture.state, 'run1', builder=fixture.builder)
            self.assertEqual(fixture.published(), fixture.v0)
            self.assertEqual(fixture.builds, 1)

    def test_completed_run_cannot_redirect_its_events_to_another_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            self.assertEqual(run(fixture.spec()).returncode, 0)
            expected = fixture.entries()
            config = Path(fixture.config)
            policy = json.loads(config.read_text())
            redirected = Path(directory) / 'redirected.jsonl'
            policy['log'] = str(redirected)
            config.write_text(json.dumps(policy))
            with self.assertRaisesRegex(Refused, 'ledger policy changed'):
                resume(fixture.state, 'run1', builder=fixture.builder)
            self.assertFalse(redirected.exists())
            self.assertEqual(fixture.entries(), expected)
            self.assertEqual(fixture.builds, 1)

    def test_policy_is_frozen_before_the_build_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            original = fixture.builder

            def builder(context):
                result = original(context)
                config = Path(fixture.config)
                policy = json.loads(config.read_text())
                policy['actor'] = 'different-actor'
                config.write_text(json.dumps(policy))
                return result

            spec = fixture.spec()
            spec.builder = builder
            with self.assertRaisesRegex(Refused, 'ledger policy changed'):
                run(spec)
            self.assertEqual(fixture.published(), fixture.v0)
            self.assertEqual(fixture.entries(), [])

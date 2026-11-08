"""A recorded publication retains its ledger policy across restart.

The policy a run started with is frozen in its journal: a change to the
configuration file while a record is pending, after the run completed, or
during the build is refused before anything else happens.
"""

import json
from pathlib import Path
import tempfile
import unittest

from eb_staged_rebuild.engine import resume, run
from eb_staged_rebuild.errors import Refused
from tests.test_ledger_directory_swap import Fixture


class LedgerPolicyTests(unittest.TestCase):
    def test_required_policy_cannot_change_while_a_record_is_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            fixture.plan_modes("fail-before")
            self.assertEqual(run(fixture.spec()).returncode, 75)
            config = Path(fixture.config)
            policy = json.loads(config.read_text())
            policy["required"] = False
            config.write_text(json.dumps(policy))
            with self.assertRaisesRegex(Refused, "ledger policy changed"):
                resume(fixture.state, "run1", builder=fixture.builder)
            self.assertEqual(fixture.payload(), "v0\n")

    def test_completed_run_cannot_redirect_its_events_to_another_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            self.assertEqual(run(fixture.spec()).returncode, 0)
            expected = fixture.entries()
            config = Path(fixture.config)
            policy = json.loads(config.read_text())
            redirected = Path(directory) / "redirected.jsonl"
            policy["log"] = str(redirected)
            config.write_text(json.dumps(policy))
            with self.assertRaisesRegex(Refused, "ledger policy changed"):
                resume(fixture.state, "run1", builder=fixture.builder)
            self.assertFalse(redirected.exists())
            self.assertEqual(fixture.entries(), expected)

    def test_policy_is_frozen_before_the_build_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            original = fixture.builder

            def builder(context):
                result = original(context)
                config = Path(fixture.config)
                policy = json.loads(config.read_text())
                policy["actor"] = "different-actor"
                config.write_text(json.dumps(policy))
                return result

            spec = fixture.spec()
            spec.builder = builder
            with self.assertRaisesRegex(Refused, "ledger policy changed"):
                run(spec)
            self.assertEqual(fixture.payload(), "v0\n")
            self.assertEqual(fixture.entries(), [])


if __name__ == "__main__":
    unittest.main()

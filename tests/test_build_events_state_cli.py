"""build-events on a damaged operation state, against TOOL_BIN only.

A state file with a field removed (truncated or edited by hand) makes the
Python tool fail with KeyError. The binary refuses it with exit 2 and names
the missing field, and never crashes. Skipped unless TOOL_BIN is set.
"""

import json
import os
import unittest

from tests.test_build_events import Base


@unittest.skipUnless(os.environ.get("TOOL_BIN"), "set TOOL_BIN to the program to test")
class DamagedStateTests(Base):
    def test_a_missing_state_field_is_refused_not_a_crash(self):
        for key in ("started", "terminal", "result", "metadata"):
            with self.subTest(missing=key):
                case = self.case(name=f"missing-{key}")
                first = case.cli(command=case.command(0))
                self.assertEqual(first.returncode, 0, first.stderr)
                path = os.path.join(case.op_dir(), "operation.json")
                state = case.state()
                del state[key]
                with open(path, "w") as handle:
                    json.dump(state, handle)
                again = case.cli(command=case.command(0))
                self.assertEqual(again.returncode, 2, again.stdout + again.stderr)
                # Without metadata the retry check refuses first, as for any
                # metadata that differs from the recorded one.
                expected = "different metadata" if key == "metadata" else f"has no {key}"
                self.assertIn(expected, again.stderr)


if __name__ == "__main__":
    unittest.main()

"""test_ledger_bounded's two receipt tests through the build-events command.

The output limit is the same 64 KiB in both implementations. A wrapper around
``fake_eb_stack.py`` pads each append's stdout to an exact size and floods
stderr. TOOL_BIN names the binary to test; without it the Python module runs.
"""

import os
import sys
import textwrap
import unittest

from eb_staged_rebuild import build_events
from eb_staged_rebuild.ledger_adapter import _OUTPUT_LIMIT
from tests.test_build_events import FAKE, Base, Case


class ReceiptLimitTests(Base):
    def padded(self, name, stdout_bytes, receipt_first=True):
        """A case whose eb-stack append prints exactly ``stdout_bytes`` on stdout.

        The receipt is the first line eb-stack 0.6 prints; ``receipt_first``
        False puts the padding before it, as a helper that chatters before
        the receipt would. Each append's stdout size goes to ``sizes``.
        """
        root = os.path.join(self.tmp, name)
        os.makedirs(root)
        self.sizes_path = os.path.join(root, "sizes")
        wrapper = os.path.join(root, "eb-stack")
        with open(wrapper, "w") as handle:
            handle.write(textwrap.dedent(f"""\
                #!{sys.executable}
                import os, subprocess, sys
                done = subprocess.run([sys.executable, {FAKE!r}, *sys.argv[1:]], capture_output=True)
                if sys.argv[1:3] == ["ledger", "append"]:
                    pad = {stdout_bytes} - len(done.stdout)
                    padding = b"p" * (pad - 1) + b"\\n" if pad > 0 else b""
                    body = done.stdout + padding if {receipt_first!r} else padding + done.stdout
                    with open({self.sizes_path!r}, "a") as handle:
                        handle.write(str(len(body)) + "\\n")
                else:
                    body = done.stdout
                for start in range(0, len(body), 4096):
                    os.write(1, body[start:start + 4096])
                chunk = b"w" * 65536
                for _ in range(128):
                    os.write(2, chunk)
                sys.exit(done.returncode)
            """))
        os.chmod(wrapper, 0o755)
        return Case(root, eb_stack=wrapper)

    def sizes(self):
        with open(self.sizes_path) as handle:
            return [int(line) for line in handle.read().split()]

    def test_receipt_that_fills_the_output_limit_is_accepted(self):
        case = self.padded("exact", _OUTPUT_LIMIT)
        case.plan_modes("fail-after-append")
        first = case.cli(command=case.command())
        self.assertEqual(first.returncode, build_events.PENDING_EXIT, first.stderr)
        self.assertEqual(case.stages(), ["started"])
        again = case.cli(command=case.command())
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(case.ran(), ["acked"])
        self.assertEqual(case.stages(), ["started", "finished"])
        started = case.events()[0]["receipt"]
        self.assertEqual((started["index"], started["replayed"]), (0, True))
        self.assertEqual(case.events()[1]["receipt"]["replayed"], False)
        self.assertEqual(self.sizes(), [_OUTPUT_LIMIT] * 3)
        case.check_unique(self)

    def test_receipt_beyond_the_output_limit_is_not_read(self):
        case = self.padded("beyond", 2 * _OUTPUT_LIMIT, receipt_first=False)
        result = case.cli(command=case.command())
        self.assertEqual(result.returncode, build_events.PENDING_EXIT, result.stderr)
        self.assertIn("printed no receipt", result.stderr)
        self.assertEqual(case.ran(), [])
        self.assertEqual(self.sizes(), [2 * _OUTPUT_LIMIT])


if __name__ == "__main__":
    unittest.main()

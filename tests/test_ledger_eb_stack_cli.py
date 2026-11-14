"""tests/test_ledger_eb_stack.py from the command line, for both implementations.

The same append, recovery and idempotence checks against a real eb-stack,
driven through ``run``, ``resume`` and ``rollback``: the run is killed
with SIGKILL after its first append returned and before the receipt was
saved (EB_STAGED_REBUILD_PAUSE_AT="ledger.acked:*"), then resumed twice
and rolled back twice. Runs the Python tool and, when TOOL_BIN is set, the
program it names. Skipped unless EB_STACK_BIN is set.

    EB_STACK_BIN=.../eb-stack TOOL_BIN=zig-out/bin/eb-staged-rebuild \\
        python3 -m unittest -v tests.test_ledger_eb_stack_cli
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from eb_staged_rebuild.journal import load_journal
from tests import test_ledger_eb_stack as base
from tests.support import classic_install

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE_EB = os.path.join(REPO, "tests", "fake_eb.py")


def implementations():
    found = [("python", [sys.executable, "-m", "eb_staged_rebuild"])]
    if os.environ.get("TOOL_BIN"):
        found.append(("binary", [os.environ["TOOL_BIN"]]))
    return found


class CliEbStackLedgerTests(base.RealEbStackLedgerTests):
    def cli_fixture(self, root):
        software = os.path.join(root, "software")
        modules = os.path.join(root, "modules")
        state = os.path.join(root, "state")
        os.makedirs(software)
        os.makedirs(modules)
        install, module, _text = classic_install(software, modules, "demo/1", "v0\n", "all/demo/1.lua")
        private, public = base._keys(root)
        log = os.path.join(root, "ledger", "stack.jsonl")
        head = os.path.join(root, "out", "head.json")
        os.makedirs(os.path.dirname(log))
        os.makedirs(os.path.dirname(head))
        config = {
            "eb_stack": os.path.abspath(base.BINARY), "log": log, "key": private, "head": head,
            "public_key": public, "actor": "swap-test", "stack": "2026.06", "target": "x86_64/generic",
            "action": "rebuild-inplace", "required": True, "timeout": 30,
        }
        config_path = os.path.join(root, "ledger.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        eb = os.path.join(root, "eb")
        with open(eb, "w", encoding="utf-8") as handle:
            handle.write(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_EB}" "$@"\n')
        os.chmod(eb, 0o755)
        args = [
            "run", "--software", software, "--modules", modules, "--state-root", state,
            "--backend", "installpath", "--eb", eb, "--run-id", "run1", "--skip-live-check",
            "--ledger-config", config_path, "--module", "demo/1", "--", "demo-1.eb",
        ]
        return args, state, install, log, head

    def payload(self, install):
        with open(os.path.join(install, "PAYLOAD"), encoding="utf-8") as handle:
            return handle.read()

    def test_append_recovery_and_idempotence(self):
        for name, command in implementations():
            with self.subTest(implementation=name), tempfile.TemporaryDirectory() as root:
                args, state, install, log, head = self.cli_fixture(root)
                env = dict(os.environ, PYTHONPATH=REPO, FAKE_EB_PAYLOAD="v1\n")
                ready = os.path.join(root, "ready")
                paused = dict(env, EB_STAGED_REBUILD_PAUSE_AT="ledger.acked:*",
                              EB_STAGED_REBUILD_PAUSE_READY=ready, EB_STAGED_REBUILD_PAUSE_SECONDS="60")
                child = subprocess.Popen(command + args, env=paused, cwd=root, start_new_session=True,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                deadline = time.monotonic() + 60
                while not os.path.exists(ready):
                    self.assertIsNone(child.poll(), "the run exited before the first receipt")
                    self.assertLess(time.monotonic(), deadline, "the run never reached ledger.acked")
                    time.sleep(0.02)
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
                self.assertEqual(self.payload(install), "v0\n")
                self.assertEqual([entry["stage"] for entry in self.entries(log)], ["started"])
                self.assertEqual(self.entries(log)[0]["action"], "rebuild-inplace")

                def tool(*words):
                    done = subprocess.run(command + list(words), env=env, cwd=root, capture_output=True, text=True)
                    self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

                tool("resume", "--state-root", state, "run1")
                self.assertEqual(self.payload(install), "v1\n")
                published = self.assert_protocol(log, head, install)
                self.assertEqual([entry["stage"] for entry in published], ["started", "finished"])
                journal = load_journal(os.path.join(state, "runs", "run1", "journal.json"))
                self.assertEqual(journal["schema"], 4)
                self.assertEqual(journal["publication"], "directory-swap")

                tool("resume", "--state-root", state, "run1")
                self.assertEqual(len(self.entries(log)), 2)

                tool("rollback", "--state-root", state, "run1")
                self.assertEqual(self.payload(install), "v0\n")
                rolled = self.assert_protocol(log, head, install)
                self.assertEqual(
                    [entry["stage"] for entry in rolled if entry["action"] == "correction"],
                    ["started", "finished"],
                )
                tool("rollback", "--state-root", state, "run1")
                self.assertEqual(len(self.entries(log)), 4)
                self.assert_protocol(log, head, install)


if __name__ == "__main__":
    unittest.main()

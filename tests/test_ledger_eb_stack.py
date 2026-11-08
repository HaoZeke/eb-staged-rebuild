"""Ledger append, recovery, and idempotence against a real eb-stack binary.

Set ``EB_STACK_BIN`` to the executable built from eb-stack 0.5.2 (tag
``v0.5.2``, c95bdcd). The test is skipped when that
variable is unset. The filesystem under test is whatever the temporary
directory uses, which on the recorded run is ext4. This is not a General
Parallel File System acceptance test.
"""

import hashlib
import json
import os
import subprocess
import tempfile
import unittest

from eb_staged_rebuild.engine import resume, rollback, run
from eb_staged_rebuild.faults import FaultInjector, InjectedCrash
from eb_staged_rebuild.journal import load_journal
from tests.support import PROBES, classic_install, installpath_builder

BINARY = os.environ.get("EB_STACK_BIN")


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _AckCrash(FaultInjector):
    """Raise after the first real append has returned and before its receipt is saved."""

    def __init__(self):
        super().__init__()
        self.tripped = False

    def reach(self, step):
        super().reach(step)
        if step.startswith("ledger.acked:") and not self.tripped:
            self.tripped = True
            raise InjectedCrash(step)


def _keys(directory):
    private = os.path.join(directory, "ledger-key.pem")
    public = os.path.join(directory, "ledger-key.pub")
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "ED25519", "-out", private],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["openssl", "pkey", "-in", private, "-pubout", "-out", public],
        check=True,
        capture_output=True,
    )
    return private, public


class RealEbStackLedgerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not BINARY:
            raise unittest.SkipTest("set EB_STACK_BIN to run the real ledger protocol")
        completed = subprocess.run(
            [BINARY, "--version"],
            check=True,
            capture_output=True,
            text=True,
        )
        cls.version = completed.stdout.strip() or completed.stderr.strip()
        cls.binary_sha256 = _sha256(BINARY)
        print(f"eb-stack binary {_sha256(BINARY)} version {cls.version}", flush=True)

    def fixture(self, root):
        software = os.path.join(root, "software")
        modules = os.path.join(root, "modules")
        state = os.path.join(root, "state")
        os.makedirs(software)
        os.makedirs(modules)
        install, module, _text = classic_install(
            software, modules, "demo/1", "v0\n", "all/demo/1.lua"
        )
        private, public = _keys(root)
        log = os.path.join(root, "ledger", "stack.jsonl")
        head = os.path.join(root, "out", "head.json")
        os.makedirs(os.path.dirname(log))
        os.makedirs(os.path.dirname(head))
        config = {
            "eb_stack": os.path.abspath(BINARY),
            "log": log,
            "key": private,
            "head": head,
            "public_key": public,
            "actor": "swap-test",
            "stack": "2026.06",
            "target": "x86_64/generic",
            "action": "rebuild-inplace",
            "required": True,
            "timeout": 30,
        }
        config_path = os.path.join(root, "ledger.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        from eb_staged_rebuild.engine import Spec

        spec = Spec(
            software=software,
            modules=modules,
            state_root=state,
            eb_args=["demo-1.eb"],
            backend="installpath",
            module_rels=["demo/1"],
            builder=installpath_builder(
                [{"rel": "demo/1", "payload": "v1\n", "module_rel": "all/demo/1.lua"}]
            ),
            faults=FaultInjector(),
            run_id="run1",
            probes=PROBES,
            ledger_config=config_path,
            clock=lambda: 1_700_000_000,
        )
        return spec, install, module, log, head

    def entries(self, log):
        with open(log, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def assert_protocol(self, log, head, install):
        with open(log, encoding="utf-8") as handle:
            raw_lines = [line for line in handle.read().splitlines() if line]
        for line in raw_lines:
            parsed_line = json.loads(line)
            canonical = json.dumps(parsed_line, separators=(",", ":"), sort_keys=True)
            self.assertEqual(canonical, line)
        parsed = [json.loads(line) for line in raw_lines]
        ids = [entry["build"]["event_id"] for entry in parsed]
        self.assertEqual(len(ids), len(set(ids)))
        with open(head, encoding="utf-8") as handle:
            signed = json.load(handle)
        self.assertEqual(signed["domain"], "eb-stack-ledger-v1")
        self.assertEqual(signed["size"], len(parsed))
        self.assertEqual(len(signed["root"]), 64)
        self.assertEqual(len(signed["signature"]), 128)
        for entry in parsed:
            self.assertEqual(entry["location"], install)
            self.assertNotIn("generations", entry["location"])
            self.assertEqual(entry["module"], "demo/1")
        return parsed

    def test_append_recovery_and_idempotence(self):
        with tempfile.TemporaryDirectory() as root:
            spec, install, _module, log, head = self.fixture(root)
            spec.faults = _AckCrash()
            with self.assertRaises(InjectedCrash):
                run(spec)
            with open(os.path.join(install, "PAYLOAD"), encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "v0\n")
            started = self.entries(log)
            self.assertEqual([entry["stage"] for entry in started], ["started"])
            self.assertEqual(started[0]["action"], "rebuild-inplace")

            resumed = resume(spec.state_root, "run1", builder=spec.builder, clock=spec.clock)
            self.assertEqual(resumed.returncode, 0, resumed.message)
            with open(os.path.join(install, "PAYLOAD"), encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "v1\n")
            published = self.assert_protocol(log, head, install)
            self.assertEqual([entry["stage"] for entry in published], ["started", "finished"])
            journal = load_journal(os.path.join(spec.state_root, "runs", "run1", "journal.json"))
            self.assertEqual(journal["schema"], 4)
            self.assertEqual(journal["publication"], "directory-swap")

            again = resume(spec.state_root, "run1", builder=spec.builder, clock=spec.clock)
            self.assertEqual(again.returncode, 0, again.message)
            self.assertEqual(len(self.entries(log)), 2)

            first = rollback(spec.state_root, "run1", clock=lambda: 1_700_000_100)
            self.assertEqual(first.returncode, 0, first.message)
            with open(os.path.join(install, "PAYLOAD"), encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "v0\n")
            rolled = self.assert_protocol(log, head, install)
            self.assertEqual(
                [entry["stage"] for entry in rolled if entry["action"] == "correction"],
                ["started", "finished"],
            )
            second = rollback(spec.state_root, "run1", clock=lambda: 1_700_000_200)
            self.assertEqual(second.returncode, 0, second.message)
            self.assertEqual(len(self.entries(log)), 4)
            self.assert_protocol(log, head, install)


if __name__ == "__main__":
    unittest.main()

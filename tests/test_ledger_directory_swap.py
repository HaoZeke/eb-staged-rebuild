"""Directory-swap publication records use the eb-stack ledger field set.

The checker mirrors ``check_fields`` in HaoZeke/eb-stack ``src/ledger.rs`` at
``79f7f8a4``. eb-stack itself is not modified or executed here.
"""

import json
import os
import sys
import tempfile
import unittest

from eb_staged_rebuild.engine import resume, rollback, run
from eb_staged_rebuild.errors import NeedsIntervention, Refused
from eb_staged_rebuild.faults import FaultInjector
from eb_staged_rebuild.journal import load_journal, save_journal
from tests.support import PROBES, classic_install, installpath_builder

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_eb_stack.py")

REQUIRED = ("ts", "actor", "stack", "target", "module", "location", "action")
ACTIONS = {
    "install", "rebuild-inplace", "rebuild-bump", "retire", "unretire",
    "move", "access", "remove", "finding", "correction",
}
STAGES = {"started", "finished", "aborted"}
LINKS = {"supersedes", "superseded_by", "moved_from", "moved_to", "corrects"}
STRUCTURED = {"reason", "easyconfig", "sources", "sbom", "modulefile", "build"}
DERIVED = {"id", "index", "sig"}
KNOWN = set(REQUIRED) | LINKS | STRUCTURED | DERIVED | {"stage"}


def assert_ledger_fields(testcase, record):
    unknown = [key for key in record if key not in KNOWN]
    testcase.assertEqual(unknown, [])
    for field in REQUIRED:
        testcase.assertIsInstance(record[field], str)
        testcase.assertTrue(record[field])
    testcase.assertIn(record["action"], ACTIONS)
    testcase.assertIn(record["stage"], STAGES)
    if record["action"] == "correction":
        testcase.assertIsInstance(record.get("corrects"), str)
        testcase.assertTrue(record["corrects"])
    event = record["build"]["event_id"]
    testcase.assertIsInstance(event, str)
    testcase.assertTrue(event.strip())


class Fixture:
    def __init__(self, root):
        self.root = root
        self.software = os.path.join(root, "software")
        self.modules = os.path.join(root, "modules")
        self.state = os.path.join(root, "state")
        os.makedirs(self.software)
        os.makedirs(self.modules)
        self.install, self.module, self.old_text = classic_install(
            self.software, self.modules, "demo/1", "v0\n", "all/demo/1.lua"
        )
        self.builder = installpath_builder(
            [{"rel": "demo/1", "payload": "v1\n", "module_rel": "all/demo/1.lua"}]
        )
        self.plan = os.path.join(root, "plan")
        self.log = os.path.join(root, "ledger", "stack.jsonl")
        self.head = os.path.join(root, "out", "head.json")
        os.makedirs(os.path.dirname(self.log))
        os.makedirs(os.path.dirname(self.head))
        eb_stack = os.path.join(root, "eb-stack")
        with open(eb_stack, "w", encoding="utf-8") as handle:
            handle.write(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        os.chmod(eb_stack, 0o755)
        os.environ["FAKE_LEDGER_PLAN"] = self.plan
        config = {
            "eb_stack": eb_stack,
            "log": self.log,
            "key": os.path.join(root, "unused.pem"),
            "head": self.head,
            "actor": "swap-test",
            "stack": "2026.06",
            "target": "x86_64/generic",
            "action": "rebuild-inplace",
            "required": True,
            "timeout": 5,
        }
        self.config = os.path.join(root, "ledger.json")
        with open(self.config, "w", encoding="utf-8") as handle:
            json.dump(config, handle)

    def plan_modes(self, *modes):
        with open(self.plan, "w", encoding="utf-8") as handle:
            handle.write("".join(mode + "\n" for mode in modes))

    def spec(self, faults=None):
        from eb_staged_rebuild.engine import Spec

        return Spec(
            software=self.software,
            modules=self.modules,
            state_root=self.state,
            eb_args=["demo-1.eb"],
            backend="installpath",
            module_rels=["demo/1"],
            builder=self.builder,
            faults=faults or FaultInjector(),
            run_id="run1",
            probes=PROBES,
            ledger_config=self.config,
            clock=lambda: 1_700_000_000,
        )

    def entries(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle]

    def payload(self):
        with open(os.path.join(self.install, "PAYLOAD"), encoding="utf-8") as handle:
            return handle.read()


class DirectorySwapLedgerTests(unittest.TestCase):
    def test_records_name_the_stable_directory_and_use_schema_4(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = Fixture(root)
            result = run(fixture.spec())
            self.assertEqual(result.returncode, 0, result.message)
            self.assertEqual(fixture.payload(), "v1\n")
            journal = load_journal(os.path.join(fixture.state, "runs", "run1", "journal.json"))
            self.assertEqual(journal["schema"], 4)
            self.assertEqual(journal["publication"], "directory-swap")
            entries = fixture.entries()
            self.assertEqual([entry["stage"] for entry in entries], ["started", "finished"])
            for entry in entries:
                assert_ledger_fields(self, entry)
                self.assertEqual(entry["module"], "demo/1")
                self.assertEqual(entry["location"], fixture.install)
                self.assertNotIn("generations", entry["location"])
                self.assertEqual(entry["action"], "rebuild-inplace")
                self.assertEqual(entry["modulefile"]["path"], fixture.module)

    def test_required_ledger_failure_does_not_swap(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = Fixture(root)
            before_inode = os.lstat(fixture.install).st_ino
            fixture.plan_modes("fail-before")
            result = run(fixture.spec())
            self.assertEqual(result.returncode, 75, result.message)
            self.assertEqual(fixture.payload(), "v0\n")
            self.assertEqual(os.lstat(fixture.install).st_ino, before_inode)
            resumed = resume(fixture.state, "run1", builder=fixture.builder)
            self.assertEqual(resumed.returncode, 0, resumed.message)
            self.assertEqual(fixture.payload(), "v1\n")
            stages = [entry["stage"] for entry in fixture.entries()]
            self.assertEqual(stages, ["started", "finished"])

    def test_policy_change_is_refused_and_the_stable_tree_stays(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = Fixture(root)
            fixture.plan_modes("fail-before")
            self.assertEqual(run(fixture.spec()).returncode, 75)
            with open(fixture.config, encoding="utf-8") as handle:
                policy = json.loads(handle.read())
            policy["actor"] = "other"
            with open(fixture.config, "w", encoding="utf-8") as handle:
                json.dump(policy, handle)
            with self.assertRaisesRegex(Refused, "ledger policy changed"):
                resume(fixture.state, "run1", builder=fixture.builder)
            self.assertEqual(fixture.payload(), "v0\n")

    def test_rollback_correction_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = Fixture(root)
            self.assertEqual(run(fixture.spec()).returncode, 0)
            first = rollback(fixture.state, "run1", clock=lambda: 1_700_000_100)
            self.assertEqual(first.returncode, 0, first.message)
            self.assertEqual(fixture.payload(), "v0\n")
            again = rollback(fixture.state, "run1", clock=lambda: 1_700_000_200)
            self.assertEqual(again.returncode, 0, again.message)
            corrections = [entry for entry in fixture.entries() if entry["action"] == "correction"]
            self.assertEqual([entry["stage"] for entry in corrections], ["started", "finished"])
            for entry in corrections:
                assert_ledger_fields(self, entry)
                self.assertEqual(entry["location"], fixture.install)
            finished = [entry for entry in fixture.entries() if entry["stage"] == "finished" and entry["action"] != "correction"]
            self.assertEqual(corrections[0]["corrects"], finished[0]["id"])

    def test_generation_pointer_schema_is_not_applied(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = Fixture(root)
            run_dir = os.path.join(fixture.state, "runs", "foreign")
            os.makedirs(run_dir)
            foreign = {
                "schema": 3,
                "publication": "generation-pointer",
                "kind": "publish",
                "run_id": "foreign",
                "state": "built",
                "software": fixture.software,
                "modules": fixture.modules,
                "state_root": fixture.state,
                "modules_plan": [
                    {
                        "rel": "all/demo/1.lua",
                        "generation": os.path.join(root, "hidden-generation"),
                        "live_module": fixture.module,
                    }
                ],
                "ledger_policy": {"actor": "x"},
            }
            with open(os.path.join(run_dir, "journal.json"), "w", encoding="utf-8") as handle:
                json.dump(foreign, handle)
            with open(fixture.module, encoding="utf-8") as handle:
                before = handle.read()
            with self.assertRaisesRegex(NeedsIntervention, "generation-pointer"):
                load_journal(os.path.join(run_dir, "journal.json"))
            with self.assertRaises(NeedsIntervention):
                resume(fixture.state, "foreign", builder=fixture.builder)
            with open(fixture.module, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), before)
            self.assertEqual(fixture.payload(), "v0\n")
            # save_journal must not rewrite this record into a directory swap.
            with self.assertRaises(NeedsIntervention):
                save_journal(os.path.join(run_dir, "journal.json"), foreign, FaultInjector(), "journal.foreign")


if __name__ == "__main__":
    unittest.main()

"""Publication events go through a local outbox to the eb-stack ledger CLI.

A crash or I/O failure before or after each journal, outbox, append and head
step is followed by resume or rollback. Every case checks that no live module
file changed before its started record was acknowledged, that each event is
in the ledger once and in order, and that a finished build is not rerun.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from eb_staged_rebuild import outbox
from eb_staged_rebuild.engine import Spec, resume, rollback, run
from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.faults import FaultInjector, InjectedCrash
from eb_staged_rebuild.ledger_adapter import EbStackLedger, load_config
from tests.support import PROBES, classic_install, generation_of, installpath_builder, lua

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_eb_stack.py")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGES = [
    {"rel": "demo/1", "payload": "v1\n", "module_rel": "all/demo/1.lua"},
    {"rel": "tool/2", "payload": "t1\n", "module_rel": "all/tool/2.lua", "name": "tool", "version": "2"},
]

class LedgerOrder(FaultInjector):
    """Fail where asked, and check that a live module file is replaced only
    after the ledger holds its acknowledged started record."""

    def __init__(self, fixture, fail_at=None, hook=None):
        super().__init__(fail_at)
        self.fixture = fixture
        self.hook = hook

    def reach(self, step):
        if step.startswith(("publish.file:", "rollback.file:", "rollback.remove:")):
            rel = step.split(":", 1)[1]
            if not step.startswith("rollback.remove:"):
                rel = rel.rsplit(".", 1)[0]
            stage = "started"
            if step.startswith("rollback"):
                stage = "rollback-started" if self.fixture.finished_acknowledged(rel) else None
            if stage is not None and not self.fixture.acknowledged(rel, stage):
                raise AssertionError(f"{step} before the {stage} record of {rel} was acknowledged")
        if self.hook is not None:
            self.hook(step)
        super().reach(re.sub(r"eb-staged-rebuild/[0-9a-f]{16}/", "eb-staged-rebuild/SCOPE/", step))

class Fixture:
    def __init__(self, root, eb_stack=None, timeout=5, required=True, public_key=None, key=None):
        self.root = root
        self.software = os.path.join(root, "software")
        self.modules = os.path.join(root, "modules")
        self.state = os.path.join(root, "state")
        for directory in (self.software, self.modules, os.path.join(root, "ledger"), os.path.join(root, "out")):
            os.makedirs(directory)
        self.v0 = {}
        self.live = {}
        for package in PACKAGES:
            _install, module, text = classic_install(
                self.software, self.modules, package["rel"], "v0\n", package["module_rel"],
                name=package.get("name", "demo"), version=package.get("version", "1"),
            )
            self.v0[package["module_rel"]] = text
            self.live[package["module_rel"]] = module
        self.builds = 0
        inner = installpath_builder(PACKAGES)

        def builder(ctx):
            self.builds += 1
            return inner(ctx)

        self.builder = builder
        self.plan = os.path.join(root, "plan")
        self.log = os.path.join(root, "ledger", "stack.jsonl")
        self.head = os.path.join(root, "out", "head.json")
        if eb_stack is None:
            eb_stack = os.path.join(root, "eb-stack")
            with open(eb_stack, "w") as handle:
                handle.write(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
            os.chmod(eb_stack, 0o755)
        os.environ["FAKE_LEDGER_PLAN"] = self.plan
        config = {
            "eb_stack": eb_stack, "log": self.log, "key": key or os.path.join(root, "unused.pem"),
            "head": self.head, "actor": "outbox-test", "stack": "2026.06",
            "target": "x86_64/generic", "required": required, "timeout": timeout,
        }
        if public_key:
            config["public_key"] = public_key
        self.config = os.path.join(root, "ledger.json")
        with open(self.config, "w") as handle:
            json.dump(config, handle)

    def plan_modes(self, *modes):
        with open(self.plan, "w") as handle:
            handle.write("".join(mode + "\n" for mode in modes))

    def spec(self, faults=None):
        return Spec(
            software=self.software, modules=self.modules, state_root=self.state,
            eb_args=["demo-1.eb"], backend="installpath", builder=self.builder,
            faults=faults or FaultInjector(), run_id="run1", probes=PROBES,
            ledger_config=self.config,
        )

    def entries(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as handle:
            return [json.loads(line) for line in handle]

    def outbox(self):
        return outbox.load(os.path.join(self.state, "runs", "run1"))

    def acknowledged(self, rel, stage):
        for event in self.outbox()["events"]:
            if event["record"]["build"]["module_file"] == rel and event["event_id"].endswith("/" + stage):
                return event["status"] == "acknowledged" and any(
                    entry["build"]["event_id"] == event["event_id"] for entry in self.entries()
                )
        return False

    def finished_acknowledged(self, rel):
        return self.acknowledged(rel, "finished")

    def published(self):
        texts = {}
        for rel, path in self.live.items():
            with open(path) as handle:
                texts[rel] = handle.read()
        return texts

    def v1(self):
        return {
            package["module_rel"]: lua(
                generation_of(self.state, "run1", package["rel"]),
                name=package.get("name", "demo"), version=package.get("version", "1"),
            )
            for package in PACKAGES
        }

    def check_ledger(self, *stages):
        ids = [entry["build"]["event_id"] for entry in self.entries()]
        assert len(ids) == len(set(ids)), f"duplicate event in ledger: {ids}"
        for package in PACKAGES:
            rel = package["module_rel"]
            seen = [entry["build"]["event_id"].rsplit("/", 1)[-1] for entry in self.entries() if entry["build"]["module_file"] == rel]
            assert seen == list(stages), f"{rel}: {seen} != {list(stages)}"

def _drive(callable_, attempts=4):
    """Call until the run reports success, as an operator would."""
    result = None
    for _ in range(attempts):
        result = callable_()
        if result.returncode == 0:
            return result
    return result

class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(os.environ.pop, "FAKE_LEDGER_PLAN", None)

    def fixture(self, name="case", **kwargs):
        root = os.path.join(self.tmp, name)
        os.makedirs(root)
        return Fixture(root, **kwargs)

    def test_clean_run_records_started_before_finished(self):
        fx = self.fixture()
        result = run(fx.spec(LedgerOrder(fx)))
        self.assertEqual(result.returncode, 0, result.message)
        self.assertEqual(fx.published(), fx.v1())
        fx.check_ledger("started", "finished")
        self.assertFalse(outbox.pending(fx.outbox()))
        record = fx.entries()[0]
        self.assertEqual(record["module"], "demo/1")
        self.assertEqual(record["location"], generation_of(fx.state, "run1", "demo/1"))
        self.assertEqual(record["action"], "rebuild-inplace")
        self.assertEqual(fx.builds, 1)

    def test_crash_at_every_step_resumes_without_duplicates_or_early_publication(self):
        probe = self.fixture("probe")
        recorder = LedgerOrder(probe)
        self.assertEqual(run(probe.spec(recorder)).returncode, 0)
        steps = list(dict.fromkeys(recorder.reached))
        for required in ("outbox.started.rename", "ledger.append:", "ledger.acked:", "outbox.acked:", "publish.file:", "journal.complete.rename", "outbox.finished.write"):
            self.assertTrue(any(step.startswith(required) for step in steps), required)
        building = steps.index("build.start")
        built = steps.index("journal.built.rename")
        for at, step in enumerate(steps):
            with self.subTest(step=step):
                fx = self.fixture(f"crash{at}")
                with self.assertRaises(InjectedCrash):
                    run(fx.spec(LedgerOrder(fx, fail_at=step)))
                journal = os.path.join(fx.state, "runs", "run1", "journal.json")
                if os.path.exists(journal):
                    result = _drive(lambda: resume(fx.state, "run1", builder=fx.builder, faults=LedgerOrder(fx)))
                    builds = 2 if building < at <= built else 1
                else:
                    result = run(fx.spec(LedgerOrder(fx)))
                    builds = 1
                self.assertEqual(result.returncode, 0, result.message)
                self.assertEqual(fx.published(), fx.v1())
                fx.check_ledger("started", "finished")
                self.assertFalse(outbox.pending(fx.outbox()))
                self.assertEqual(fx.builds, builds, step)

    def test_publisher_failure_at_each_event_leaves_live_files_until_started_is_acknowledged(self):
        modes = ("fail-before", "fail-after-append", "garbage", "stale-head", "hang")
        for mode in modes:
            for position in range(4):
                with self.subTest(mode=mode, position=position):
                    fx = self.fixture(f"{mode}{position}", timeout=1)
                    fx.plan_modes(*(["ok"] * position), mode)
                    result = run(fx.spec(LedgerOrder(fx)))
                    self.assertEqual(result.returncode, outbox.PENDING_EXIT, result.message)
                    if position < 2:
                        self.assertEqual(result.state, "built")
                        self.assertEqual(fx.published(), fx.v0)
                    else:
                        self.assertEqual(result.state, "complete")
                        self.assertEqual(fx.published(), fx.v1())
                    pending = outbox.pending(fx.outbox())
                    self.assertTrue(pending)
                    self.assertTrue(pending[0]["error"])
                    result = resume(fx.state, "run1", builder=fx.builder, faults=LedgerOrder(fx))
                    self.assertEqual(result.returncode, 0, result.message)
                    self.assertEqual(fx.published(), fx.v1())
                    fx.check_ledger("started", "finished")
                    self.assertEqual(fx.builds, 1)
                    replayed = [event["receipt"]["replayed"] for event in fx.outbox()["events"]]
                    self.assertEqual(any(replayed), mode in ("fail-after-append", "garbage", "stale-head"))

    def test_status_reports_outbox_progress(self):
        fx = self.fixture(timeout=1)
        fx.plan_modes("ok", "fail-before")
        self.assertEqual(run(fx.spec()).returncode, outbox.PENDING_EXIT)
        out = subprocess.run(
            [sys.executable, "-m", "eb_staged_rebuild", "status", "--state-root", fx.state],
            capture_output=True, text=True, cwd=REPO, check=True,
        ).stdout
        self.assertIn("ledger events: 1/2 acknowledged", out)
        self.assertIn("live modules were not changed", out)

    def test_optional_ledger_publishes_and_keeps_events_pending(self):
        fx = self.fixture(required=False, timeout=1)
        fx.plan_modes("fail-before", "fail-before")
        result = run(fx.spec())
        self.assertEqual(result.returncode, outbox.PENDING_EXIT)
        self.assertEqual(result.state, "complete")
        self.assertEqual(fx.published(), fx.v1())
        self.assertEqual(fx.entries(), [])
        self.assertEqual(len(outbox.pending(fx.outbox())), 4)
        self.assertEqual(resume(fx.state, "run1").returncode, 0)
        fx.check_ledger("started", "finished")

    def test_rollback_after_publication_records_a_correction_before_restoring(self):
        fx = self.fixture()
        self.assertEqual(run(fx.spec()).returncode, 0)
        finished = {entry["build"]["module_file"]: entry["id"] for entry in fx.entries() if entry["stage"] == "finished"}
        fx.plan_modes("fail-before")
        result = rollback(fx.state, "run1", faults=LedgerOrder(fx))
        self.assertEqual(result.returncode, outbox.PENDING_EXIT, result.message)
        self.assertEqual(fx.published(), fx.v1())
        result = rollback(fx.state, "run1", faults=LedgerOrder(fx))
        self.assertEqual(result.returncode, 0, result.message)
        self.assertEqual(fx.published(), fx.v0)
        fx.check_ledger("started", "finished", "rollback-started", "rollback-finished")
        for entry in fx.entries():
            if entry["action"] == "correction":
                self.assertEqual(entry["corrects"], finished[entry["build"]["module_file"]])
        self.assertEqual(rollback(fx.state, "run1").returncode, 0)
        fx.check_ledger("started", "finished", "rollback-started", "rollback-finished")

    def test_rollback_of_an_unfinished_publication_records_aborted(self):
        fx = self.fixture()
        rel = PACKAGES[1]["module_rel"]
        with self.assertRaises(InjectedCrash):
            run(fx.spec(LedgerOrder(fx, fail_at=f"publish.file:{rel}.rename")))
        self.assertEqual(rollback(fx.state, "run1", faults=LedgerOrder(fx)).returncode, 0)
        self.assertEqual(fx.published(), fx.v0)
        fx.check_ledger("started", "aborted")
        with self.assertRaises(Refused):
            resume(fx.state, "run1", builder=fx.builder)

    def test_crash_at_every_rollback_step_converges(self):
        probe = self.fixture("probe")
        self.assertEqual(run(probe.spec()).returncode, 0)
        recorder = LedgerOrder(probe)
        self.assertEqual(rollback(probe.state, "run1", faults=recorder).returncode, 0)
        steps = list(dict.fromkeys(recorder.reached))
        self.assertTrue(any(step.startswith("rollback.file:") for step in steps))
        for at, step in enumerate(steps):
            with self.subTest(step=step):
                fx = self.fixture(f"rollback{at}")
                self.assertEqual(run(fx.spec()).returncode, 0)
                with self.assertRaises(InjectedCrash):
                    rollback(fx.state, "run1", faults=LedgerOrder(fx, fail_at=step))
                result = _drive(lambda: rollback(fx.state, "run1", faults=LedgerOrder(fx)))
                self.assertEqual(result.returncode, 0, result.message)
                self.assertEqual(fx.published(), fx.v0)
                fx.check_ledger("started", "finished", "rollback-started", "rollback-finished")

    def test_reused_run_identifier_with_different_content_is_never_acknowledged(self):
        fx = self.fixture()
        self.assertEqual(run(fx.spec()).returncode, 0)
        run_dir = os.path.join(fx.state, "runs", "run1")
        data = fx.outbox()
        data["events"][-1]["status"] = "pending"
        data["events"][-1]["record"]["reason"] = "different"
        with open(outbox.outbox_path(run_dir), "w") as handle:
            json.dump(data, handle)
        result = resume(fx.state, "run1")
        self.assertEqual(result.returncode, outbox.PENDING_EXIT)
        self.assertIn("different content", outbox.pending(fx.outbox())[0]["error"])
        self.assertEqual(len(fx.entries()), 4)

    def test_config_is_checked_before_anything_is_built(self):
        fx = self.fixture()
        with open(fx.config, "w") as handle:
            json.dump({"eb_stack": "relative"}, handle)
        with self.assertRaises(Refused):
            run(fx.spec())
        self.assertEqual(fx.builds, 0)
        self.assertFalse(os.path.exists(os.path.join(fx.state, "runs")))

@unittest.skipUnless(
    os.environ.get("EB_STACK_BIN") and shutil.which("openssl"),
    "set EB_STACK_BIN to an eb-stack binary (with openssl on PATH) to drive the real ledger",
)
class RealLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(os.environ.pop, "FAKE_LEDGER_PLAN", None)
        keys = os.path.join(self.tmp, "keys")
        os.makedirs(keys)
        self.key = os.path.join(keys, "ledger.pem")
        self.pub = os.path.join(keys, "ledger.pub")
        subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", self.key], check=True, capture_output=True)
        subprocess.run(["openssl", "pkey", "-in", self.key, "-pubout", "-out", self.pub], check=True, capture_output=True)
        os.chmod(self.key, stat.S_IRUSR)
        root = os.path.join(self.tmp, "case")
        os.makedirs(root)
        self.fx = Fixture(root, eb_stack=os.path.abspath(os.environ["EB_STACK_BIN"]), key=self.key, public_key=self.pub, timeout=60)

    def verify(self, record):
        return subprocess.run(
            [os.environ["EB_STACK_BIN"], "ledger", "verify", "--log", self.fx.log, "--public-key", self.pub,
             "--record", str(record), "--head", self.fx.head],
            capture_output=True, text=True,
        )

    def test_head_failure_after_append_is_resumed_as_an_acknowledged_retry(self):
        fx = self.fx
        blocked = []

        def block_head(step):
            if step.startswith("ledger.append:") and step.endswith("tool/2.lua/finished") and not blocked:
                os.replace(fx.head, fx.head + ".saved")
                os.makedirs(os.path.join(fx.head, "occupied"))
                blocked.append(step)

        result = run(fx.spec(LedgerOrder(fx, hook=block_head)))
        self.assertEqual(result.returncode, outbox.PENDING_EXIT, result.message)
        self.assertEqual(result.state, "complete")
        self.assertEqual(len(fx.entries()), 4, "the finished line was appended before the head failed")
        self.assertIn("write head", outbox.pending(fx.outbox())[0]["error"])
        shutil.rmtree(fx.head)
        os.replace(fx.head + ".saved", fx.head)
        result = resume(fx.state, "run1")
        self.assertEqual(result.returncode, 0, result.message)
        fx.check_ledger("started", "finished")
        receipts = [event["receipt"] for event in fx.outbox()["events"]]
        self.assertTrue(receipts[-1]["replayed"])
        self.assertTrue(all(receipt["verified"] for receipt in receipts))
        for index in range(4):
            self.assertEqual(self.verify(index).returncode, 0)
        self.assertEqual(rollback(fx.state, "run1").returncode, 0)
        fx.check_ledger("started", "finished", "rollback-started", "rollback-finished")
        self.assertEqual(self.verify(7).returncode, 0)

    def test_receipt_requires_a_matching_signed_head(self):
        config = load_config(self.fx.config)
        record = os.path.join(self.tmp, "record.json")
        with open(record, "w") as handle:
            json.dump({
                "ts": "2026-11-05T00:00:00Z", "actor": "outbox-test", "stack": "2026.06",
                "target": "x86_64/generic", "module": "demo/1", "location": "/x", "action": "install",
                "build": {"event_id": "receipt-check"},
            }, handle)
        receipt = EbStackLedger(config).append(record)
        self.assertEqual((receipt["index"], receipt["head_size"], receipt["replayed"]), (0, 1, False))
        again = EbStackLedger(config).append(record)
        self.assertEqual((again["index"], again["replayed"]), (0, True))

if __name__ == "__main__":
    unittest.main()

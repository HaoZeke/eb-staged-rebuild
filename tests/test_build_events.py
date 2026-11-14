"""Build events: started is acknowledged before the command runs, finished or aborted after.

The command is a real subprocess that reports whether the ledger already held
its started record when it ran. Ledger faults come from ``fake_eb_stack.py``
modes, from wrappers around the real eb-stack binary, and from crash points
in the outbox. Every case checks the ledger holds each event at most once.
"""

import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

from eb_staged_rebuild import build_events, outbox
from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.faults import FaultInjector, InjectedCrash
from tests.test_command_group_exit import group_members

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE = os.path.join(REPO, "tests", "fake_eb_stack.py")
# TOOL_BIN runs the command-line cases against another build of the tool.
CLI = ([os.environ["TOOL_BIN"], "build-events"] if os.environ.get("TOOL_BIN")
       else [sys.executable, "-m", "eb_staged_rebuild.build_events"])
OPERATION = "job-42.zlib"
LEDGER_FIELDS = {
    "ts", "actor", "stack", "target", "module", "location", "action", "stage", "id", "index",
    "reason", "easyconfig", "sources", "sbom", "modulefile", "build",
    "supersedes", "superseded_by", "moved_from", "moved_to", "corrects", "sig",
}

# argv: log event_id runs_file modulefile artifact exit_code
COMMAND = textwrap.dedent("""\
    import json, os, sys
    log, event, runs, modulefile, artifact, code = sys.argv[1:7]
    seen = []
    if os.path.exists(log):
        with open(log) as handle:
            seen = [json.loads(line)["build"]["event_id"] for line in handle]
    with open(runs, "a") as handle:
        handle.write(("acked" if event in seen else "UNACKED") + "\\n")
    with open(modulefile, "w") as handle:
        handle.write("-- generated v2\\n")
    with open(artifact, "wb") as handle:
        handle.write(b"artifact-bytes")
    sys.exit(int(code))
""")


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Case:
    """One ledger, outbox, easyconfig and module file, under ``root``."""

    def __init__(self, root, eb_stack=None, key=None, public_key=None, timeout=5, required=True):
        self.root = root
        for name in ("ledger", "keys", "outbox", "work"):
            os.makedirs(os.path.join(root, name))
        self.log = os.path.join(root, "ledger", "stack.jsonl")
        self.runs = os.path.join(root, "work", "runs")
        self.modulefile = os.path.join(root, "work", "zlib-1.3.1.lua")
        self.artifact = os.path.join(root, "work", "zlib.tar.zst")
        self.easyconfig = os.path.join(root, "work", "zlib-1.3.1.eb")
        with open(self.easyconfig, "w") as handle:
            handle.write("name = 'zlib'\nversion = '1.3.1'\n")
        with open(self.modulefile, "w") as handle:
            handle.write("-- previous v1\n")
        self.plan = os.path.join(root, "plan")
        self.pids = os.path.join(root, "eb-stack.pids")
        if eb_stack is None:
            eb_stack = os.path.join(root, "eb-stack")
            with open(eb_stack, "w") as handle:
                handle.write(f'#!/bin/sh\necho $$ >> "{self.pids}"\nexec "{sys.executable}" "{FAKE}" "$@"\n')
            os.chmod(eb_stack, 0o755)
        if key is None:
            key = os.path.join(root, "keys", "unused.pem")
            with open(key, "w") as handle:
                handle.write("unused by the fake\n")
        os.environ["FAKE_LEDGER_PLAN"] = self.plan
        self.config = {
            "eb_stack": eb_stack, "log": self.log, "key": key,
            "outbox": os.path.join(root, "outbox"), "timeout": timeout, "required": required,
        }
        if public_key:
            self.config["public_key"] = public_key
        self.metadata = {
            "operation": OPERATION,
            "actor": "jenkins/job/42",
            "stack": "2026.06",
            "target": {"cpu": "x86_64/amd/zen4", "accelerator": "nvidia/cc90"},
            "module": "zlib/1.3.1-GCCcore-14.2.0",
            "location": "/cvmfs/example/software/zlib/1.3.1-GCCcore-14.2.0",
            "action": "rebuild-inplace",
            "easyconfig": {"path": self.easyconfig},
            "sources": [{"name": "zlib-1.3.1.tar.gz", "sha256": sha(b"source")}],
            "patches": [{"name": "zlib-1.3.1_fix.patch", "sha256": sha(b"patch")}],
            "modulefile": {"path": self.modulefile},
            "tool": {"name": "easybuild", "version": "5.1.0", "revision": "0123abcd"},
            "logs": [{"uri": "https://ci.example.org/job/42/console", "sha256": sha(b"log")}],
            "artifact": {"path": self.artifact, "uri": "https://store.example.org/zlib.tar.zst"},
        }
        self.write()

    def write(self):
        self.config_path = os.path.join(self.root, "config.json")
        self.metadata_path = os.path.join(self.root, "metadata.json")
        with open(self.config_path, "w") as handle:
            json.dump(self.config, handle)
        with open(self.metadata_path, "w") as handle:
            json.dump(self.metadata, handle)

    def plan_modes(self, *modes):
        with open(self.plan, "w") as handle:
            handle.write("".join(mode + "\n" for mode in modes))

    def command(self, code=0, operation=OPERATION):
        return [sys.executable, "-c", COMMAND, self.log, build_events.event_id(operation, "started"),
                self.runs, self.modulefile, self.artifact, str(code)]

    def cli(self, *extra, command=None, env=None, timeout=120):
        argv = [*CLI,
                "--config", self.config_path, "--metadata", self.metadata_path, *extra]
        if command is not None:
            argv += ["--", *command]
        return subprocess.run(argv, capture_output=True, text=True, cwd=REPO, timeout=timeout,
                              env=env if env is not None else dict(os.environ))

    def popen(self, command, env=None):
        argv = [*CLI,
                "--config", self.config_path, "--metadata", self.metadata_path, "--", *command]
        return subprocess.Popen(argv, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env=env if env is not None else dict(os.environ))

    def entries(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as handle:
            return [json.loads(line) for line in handle]

    def ids(self):
        return [entry["build"]["event_id"] for entry in self.entries()]

    def ran(self):
        if not os.path.exists(self.runs):
            return []
        with open(self.runs) as handle:
            return handle.read().split()

    def op_dir(self, operation=OPERATION):
        return os.path.join(self.config["outbox"], operation)

    def events(self, operation=OPERATION):
        return outbox.load(self.op_dir(operation))["events"]

    def state(self, operation=OPERATION):
        with open(os.path.join(self.op_dir(operation), "operation.json")) as handle:
            return json.load(handle)

    def stages(self, operation=OPERATION):
        prefix = f"eb-build-events/{operation}/"
        return [entry.rsplit("/", 1)[1] for entry in self.ids() if entry.startswith(prefix)]

    def check_unique(self, test):
        ids = self.ids()
        test.assertEqual(len(ids), len(set(ids)), f"duplicate event in ledger: {ids}")
        for entry in self.entries():
            test.assertLessEqual(set(entry), LEDGER_FIELDS)

    def run_api(self, code=0, faults=None):
        return build_events.run_build(self.config, self.metadata, self.command(code), faults=faults)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(os.environ.pop, "FAKE_LEDGER_PLAN", None)

    def case(self, name="case", **kwargs):
        root = os.path.join(self.tmp, name)
        os.makedirs(root)
        return Case(root, **kwargs)


class LifecycleTests(Base):
    def test_started_is_in_the_ledger_before_the_command_and_finished_after(self):
        case = self.case()
        result = case.cli(command=case.command())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(case.ran(), ["acked"])
        self.assertEqual(case.stages(), ["started", "finished"])
        case.check_unique(self)
        started, finished = case.entries()
        self.assertEqual(started["target"], "x86_64/amd/zen4/accel/nvidia/cc90")
        self.assertEqual(started["build"]["cpu_target"], "x86_64/amd/zen4")
        self.assertEqual(started["build"]["accelerator"], "nvidia/cc90")
        self.assertEqual(started["action"], "rebuild-inplace")
        with open(case.easyconfig, "rb") as handle:
            self.assertEqual(started["easyconfig"], {"path": case.easyconfig, "sha256": sha(handle.read())})
        self.assertEqual(started["sources"]["files"], case.metadata["sources"])
        self.assertEqual(started["sources"]["patches"], case.metadata["patches"])
        self.assertEqual(started["modulefile"], {"path": case.modulefile, "previous_sha256": sha(b"-- previous v1\n")})
        self.assertEqual(started["build"]["tool"], case.metadata["tool"])
        self.assertEqual(started["build"]["logs"], case.metadata["logs"])
        self.assertNotIn("artifact", started["build"])
        self.assertEqual(finished["modulefile"]["sha256"], sha(b"-- generated v2\n"))
        self.assertEqual(finished["modulefile"]["previous_sha256"], sha(b"-- previous v1\n"))
        self.assertEqual(finished["build"]["artifact"], {
            "accession": "sha256:" + sha(b"artifact-bytes"), "size": len(b"artifact-bytes"),
            "path": case.artifact, "uri": "https://store.example.org/zlib.tar.zst",
        })
        self.assertEqual(finished["build"]["exit_status"], 0)
        self.assertEqual(finished["build"]["started_id"], started["id"])
        self.assertEqual(case.state()["phase"], "exited")
        self.assertTrue(all(event["status"] == "acknowledged" for event in case.events()))

    def test_command_failure_records_aborted_and_keeps_its_status(self):
        case = self.case()
        result = case.cli(command=case.command(3))
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(case.stages(), ["started", "aborted"])
        aborted = case.entries()[1]
        self.assertEqual((aborted["build"]["outcome"], aborted["build"]["exit_status"]), ("exited", 3))
        self.assertEqual(aborted["build"]["started_id"], case.entries()[0]["id"])
        self.assertEqual(aborted["reason"], "the command exited 3")
        self.assertNotIn("artifact", aborted["build"])
        self.assertNotIn("sha256", aborted["modulefile"])
        again = case.cli(command=case.command(3))
        self.assertEqual(again.returncode, 3, again.stderr)
        self.assertIn("the command was not repeated", again.stderr)
        self.assertEqual(case.ran(), ["acked"], "a recorded command is never repeated")
        self.assertEqual(case.stages(), ["started", "aborted"])

    def test_failure_with_a_lost_aborted_ack_keeps_the_command_status_and_names_the_ledger_error(self):
        case = self.case()
        case.plan_modes("ok", "fail-before")
        result = case.cli(command=case.command(4))
        self.assertEqual(result.returncode, 4)
        self.assertIn("ledger error: 1 ledger event(s) not acknowledged", result.stderr)
        self.assertEqual(case.stages(), ["started"])
        self.assertTrue(outbox.pending({"events": case.events()})[0]["error"])
        delivered = case.cli("--deliver")
        self.assertEqual(delivered.returncode, 4, delivered.stderr)
        self.assertEqual(case.stages(), ["started", "aborted"])
        self.assertEqual(case.ran(), ["acked"])

    def test_success_with_a_lost_finished_ack_is_not_reported_as_success(self):
        for mode in ("fail-before", "fail-after-append", "garbage", "stale-head"):
            with self.subTest(mode=mode):
                case = self.case(mode)
                case.plan_modes("ok", mode)
                result = case.cli(command=case.command())
                self.assertEqual(result.returncode, build_events.PENDING_EXIT, result.stderr)
                self.assertIn("ledger error", result.stderr)
                self.assertEqual(case.ran(), ["acked"])
                again = case.cli(command=case.command())
                self.assertEqual(again.returncode, 0, again.stderr)
                self.assertEqual(case.ran(), ["acked"])
                self.assertEqual(case.stages(), ["started", "finished"])
                case.check_unique(self)

    def test_missing_executable_is_aborted_not_started(self):
        case = self.case()
        result = case.cli(command=[os.path.join(case.root, "no-such-build-tool")])
        self.assertEqual(result.returncode, build_events.NOT_STARTED_EXIT, result.stderr)
        self.assertEqual(case.stages(), ["started", "aborted"])
        self.assertEqual(case.entries()[1]["build"]["outcome"], "not-started")

    def test_outputs_that_do_not_check_turn_a_zero_exit_into_aborted(self):
        case = self.case()
        os.unlink(case.modulefile)
        case.metadata["modulefile"] = {"path": case.modulefile}
        case.write()
        result = case.cli(command=[sys.executable, "-c", "pass"])
        self.assertEqual(result.returncode, build_events.POSTCHECK_EXIT, result.stderr)
        aborted = case.entries()[1]
        self.assertEqual((aborted["stage"], aborted["build"]["outcome"], aborted["build"]["exit_status"]),
                         ("aborted", "modulefile-unreadable", 0))
        other = self.case("mismatch")
        other.metadata["artifact"] = {"path": other.artifact, "accession": "sha256:" + "0" * 64}
        other.write()
        result = other.cli(command=other.command())
        self.assertEqual(result.returncode, build_events.POSTCHECK_EXIT, result.stderr)
        self.assertEqual(other.entries()[1]["build"]["outcome"], "artifact-mismatch")

    def test_optional_mode_runs_the_command_and_keeps_events_pending(self):
        case = self.case(required=False)
        case.plan_modes("fail-before", "fail-before")
        result = case.cli(command=case.command())
        self.assertEqual(result.returncode, build_events.PENDING_EXIT, result.stderr)
        self.assertEqual(case.ran(), ["UNACKED"])
        self.assertEqual(case.entries(), [])
        self.assertEqual(len(outbox.pending({"events": case.events()})), 2)
        self.assertEqual(case.cli("--deliver").returncode, 0)
        self.assertEqual(case.stages(), ["started", "finished"])
        self.assertNotIn("started_id", case.entries()[1]["build"])


class AcknowledgementTests(Base):
    def test_lost_started_ack_blocks_the_command_until_a_retry_is_acknowledged(self):
        replays = {"fail-before": False, "fail-after-append": True, "garbage": True, "stale-head": True, "hang": False}
        for mode, replayed in replays.items():
            with self.subTest(mode=mode):
                case = self.case(mode, timeout=1)
                case.plan_modes(mode)
                result = case.cli(command=case.command())
                self.assertEqual(result.returncode, build_events.PENDING_EXIT, result.stderr)
                self.assertIn("command not run", result.stderr)
                self.assertEqual(case.ran(), [], "the command ran without an acknowledged started record")
                self.assertEqual(case.state()["phase"], "created")
                pending = outbox.pending({"events": case.events()})
                self.assertEqual(len(pending), 1)
                self.assertTrue(pending[0]["error"])
                self.assertEqual(case.stages(), ["started"] if mode in ("fail-after-append", "garbage", "stale-head") else [])
                retry = case.cli(command=case.command())
                self.assertEqual(retry.returncode, 0, retry.stderr)
                self.assertEqual(case.ran(), ["acked"])
                self.assertEqual(case.stages(), ["started", "finished"])
                self.assertEqual(case.events()[0]["receipt"]["replayed"], replayed)
                self.assertEqual(case.events()[0]["attempts"], 2)
                case.check_unique(self)
                with open(case.pids) as handle:
                    for pid in handle.read().split():
                        self.assertEqual(group_members(int(pid)), [])

    def test_crash_before_and_after_the_append_is_retried_with_the_same_bytes(self):
        started = build_events.event_id(OPERATION, "started")
        for step, in_ledger in ((f"ledger.append:{started}", False), (f"ledger.acked:{started}", True)):
            with self.subTest(step=step):
                case = self.case(step.split(":")[0])
                with self.assertRaises(InjectedCrash):
                    case.run_api(faults=FaultInjector(fail_at=step))
                self.assertEqual(case.ran(), [])
                self.assertEqual(case.stages(), ["started"] if in_ledger else [])
                body = case.events()[0]["record"]
                result = case.cli(command=case.command())
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(case.events()[0]["record"], body)
                self.assertEqual(case.entries()[0]["ts"], body["ts"])
                self.assertEqual(case.events()[0]["receipt"]["replayed"], in_ledger)
                self.assertEqual(case.ran(), ["acked"])
                self.assertEqual(case.stages(), ["started", "finished"])

    def test_exact_replay_appends_nothing_and_does_not_rerun(self):
        case = self.case()
        self.assertEqual(case.cli(command=case.command()).returncode, 0)
        with open(case.log, "rb") as handle:
            before = handle.read()
        receipts = [event["receipt"] for event in case.events()]
        again = case.cli(command=case.command())
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(case.ran(), ["acked"])
        data = outbox.load(case.op_dir())
        data["events"][1]["status"] = "pending"
        with open(outbox.outbox_path(case.op_dir()), "w") as handle:
            json.dump(data, handle)
        delivered = case.cli("--deliver")
        self.assertEqual(delivered.returncode, 0, delivered.stderr)
        with open(case.log, "rb") as handle:
            self.assertEqual(handle.read(), before)
        self.assertTrue(case.events()[1]["receipt"]["replayed"])
        self.assertEqual(case.events()[1]["receipt"]["id"], receipts[1]["id"])
        self.assertEqual(case.events()[0]["receipt"], receipts[0])

    def test_changed_metadata_is_refused_before_anything_is_appended(self):
        case = self.case()
        case.plan_modes("fail-before")
        self.assertEqual(case.cli(command=case.command()).returncode, build_events.PENDING_EXIT)
        case.metadata["actor"] = "someone-else"
        case.write()
        result = case.cli(command=case.command())
        self.assertEqual(result.returncode, build_events.REFUSED_EXIT)
        self.assertIn("different metadata", result.stderr)
        self.assertEqual((case.entries(), case.ran()), ([], []))
        case.metadata["actor"] = "jenkins/job/42"
        case.write()
        with open(case.easyconfig, "a") as handle:
            handle.write("# edited\n")
        result = case.cli(command=case.command())
        self.assertEqual(result.returncode, build_events.REFUSED_EXIT)
        self.assertIn("different metadata", result.stderr)
        self.assertEqual((case.entries(), case.ran()), ([], []))
        changed = dict(case.config, required=False)
        with self.assertRaisesRegex(Refused, "another ledger policy"):
            build_events.run_build(changed, case.metadata, case.command())

    def test_reused_operation_with_another_payload_is_refused_by_the_ledger(self):
        case = self.case()
        self.assertEqual(case.cli(command=case.command()).returncode, 0)
        case.config["outbox"] = os.path.join(case.root, "outbox-2")
        case.metadata["module"] = "zlib/1.3.2-GCCcore-14.2.0"
        case.write()
        result = case.cli(command=case.command())
        self.assertEqual(result.returncode, build_events.PENDING_EXIT, result.stderr)
        self.assertIn("different content", outbox.pending({"events": case.events()})[0]["error"])
        self.assertEqual(case.ran(), ["acked"], "the second command never ran")
        self.assertEqual(case.stages(), ["started", "finished"])

    def test_tampered_outbox_record_is_never_acknowledged(self):
        case = self.case()
        case.plan_modes("fail-after-append")
        self.assertEqual(case.cli(command=case.command()).returncode, build_events.PENDING_EXIT)
        data = outbox.load(case.op_dir())
        data["events"][0]["record"]["reason"] = "rewritten"
        with open(outbox.outbox_path(case.op_dir()), "w") as handle:
            json.dump(data, handle)
        result = case.cli(command=case.command())
        self.assertEqual(result.returncode, build_events.PENDING_EXIT)
        self.assertIn("different content", outbox.pending({"events": case.events()})[0]["error"])
        self.assertEqual(case.ran(), [])
        self.assertEqual(len(case.entries()), 1)

    def test_unreachable_ledger_blocks_the_command(self):
        case = self.case()
        case.config["eb_stack"] = os.path.join(case.root, "missing-eb-stack")
        case.write()
        result = case.cli(command=case.command())
        self.assertEqual(result.returncode, build_events.PENDING_EXIT)
        self.assertIn("cannot start", result.stderr)
        self.assertEqual(case.ran(), [])

    def test_crash_at_every_step_converges_without_duplicates_or_early_commands(self):
        probe = self.case("probe")
        recorder = FaultInjector()
        self.assertEqual(probe.run_api(faults=recorder).returncode, 0)
        steps = list(dict.fromkeys(recorder.reached))
        for required in ("build_events.created.rename", "outbox.started.rename", "ledger.append:", "ledger.acked:",
                         "build_events.running.rename", "build_events.command", "build_events.exited.rename",
                         "outbox.terminal.rename"):
            self.assertTrue(any(step.startswith(required) for step in steps), required)
        running = steps.index("build_events.running.rename")
        command = steps.index("build_events.command")
        exited = steps.index("build_events.exited.rename")
        for at, step in enumerate(steps):
            with self.subTest(step=step):
                case = self.case(f"crash{at}")
                with self.assertRaises(InjectedCrash):
                    case.run_api(faults=FaultInjector(fail_at=step))
                self.assertIn(case.ran(), ([], ["acked"]))
                result = case.run_api()
                case.check_unique(self)
                self.assertEqual(outbox.pending({"events": case.events()}), [])
                if running < at <= exited:
                    self.assertEqual(result.returncode, build_events.UNKNOWN_EXIT, result.message)
                    self.assertEqual(case.stages(), ["started", "aborted"])
                    self.assertEqual(case.entries()[1]["build"]["outcome"], "unknown")
                    self.assertEqual(case.ran(), ["acked"] if at > command else [])
                else:
                    self.assertEqual(result.returncode, 0, result.message)
                    self.assertEqual(case.stages(), ["started", "finished"])
                    self.assertEqual(case.ran(), ["acked"])


class ProcessTests(Base):
    def test_interrupted_command_is_aborted_after_its_whole_group_stopped(self):
        case = self.case()
        leader, ready = os.path.join(case.root, "leader"), os.path.join(case.root, "ready")
        worker = textwrap.dedent(f"""\
            import os, signal, time
            open({leader!r}, "w").write(str(os.getpid()))
            for _ in range(8):
                if os.fork() == 0:
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    while True:
                        time.sleep(0.01)
            open({ready!r}, "w").close()
            while True:
                time.sleep(0.05)
        """)
        child = case.popen([sys.executable, "-c", worker])
        deadline = time.monotonic() + 30
        while not os.path.exists(ready) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(os.path.exists(ready))
        child.send_signal(signal.SIGTERM)
        _out, err = child.communicate(timeout=60)
        self.assertEqual(child.returncode, 128 + signal.SIGTERM, err)
        with open(leader) as handle:
            self.assertEqual(group_members(int(handle.read())), [])
        self.assertEqual(case.stages(), ["started", "aborted"])
        self.assertEqual(case.entries()[1]["build"]["exit_status"], 128 + signal.SIGTERM)

    def test_interrupt_during_started_delivery_stops_the_ledger_group_and_never_runs_the_command(self):
        case = self.case(timeout=60)
        case.plan_modes("hang")
        child = case.popen(case.command())
        deadline = time.monotonic() + 30
        while not os.path.exists(case.pids) and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.3)
        child.send_signal(signal.SIGTERM)
        _out, err = child.communicate(timeout=60)
        self.assertEqual(child.returncode, 128 + signal.SIGTERM, err)
        with open(case.pids) as handle:
            self.assertEqual(group_members(int(handle.read().split()[0])), [])
        self.assertEqual(case.ran(), [])
        self.assertEqual(case.state()["phase"], "created")
        result = case.cli(command=case.command())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(case.ran(), ["acked"])

    def test_producer_killed_during_the_command_is_recorded_as_unknown_and_not_rerun(self):
        case = self.case()
        ready = os.path.join(case.root, "ready")
        child = case.popen([sys.executable, "-c", f"open({ready!r}, 'w').close()\nimport time\ntime.sleep(5)"])
        deadline = time.monotonic() + 30
        while not os.path.exists(ready) and time.monotonic() < deadline:
            time.sleep(0.02)
        child.kill()
        child.communicate()
        time.sleep(5.5)
        result = case.cli(command=case.command())
        self.assertEqual(result.returncode, build_events.UNKNOWN_EXIT, result.stderr)
        self.assertEqual(case.ran(), [])
        self.assertEqual(case.stages(), ["started", "aborted"])
        self.assertEqual(case.entries()[1]["build"]["outcome"], "unknown")

    def test_a_second_writer_of_the_same_operation_is_refused_while_the_first_holds_it(self):
        case = self.case()
        release = os.path.join(case.root, "release")
        hold = [sys.executable, "-c",
                f"import os, time\nwhile not os.path.exists({release!r}): time.sleep(0.02)\n"
                f"open({case.artifact!r}, 'wb').write(b'held')\n"]
        first = case.popen(hold)
        deadline = time.monotonic() + 30
        while case.stages() != ["started"] and time.monotonic() < deadline:
            time.sleep(0.02)
        second = case.cli(command=case.command())
        self.assertEqual(second.returncode, build_events.REFUSED_EXIT)
        self.assertIn("holds the lock", second.stderr)
        open(release, "w").close()
        _out, err = first.communicate(timeout=60)
        self.assertEqual(first.returncode, 0, err)
        self.assertEqual(case.ran(), [])
        self.assertEqual(case.stages(), ["started", "finished"])

    def test_environment_and_arguments_are_never_serialized(self):
        case = self.case()
        secret_env, secret_arg = "env-token-6f1c2b", "licence-value-93ad01"
        env = dict(os.environ, BUILD_SECRET_TOKEN=secret_env, LM_LICENSE_FILE=secret_env)
        command = [sys.executable, "-c", COMMAND, case.log, build_events.event_id(OPERATION, "started"),
                   case.runs, case.modulefile, case.artifact, "0", f"--licence={secret_arg}"]
        result = case.cli(command=command, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        for directory in (case.config["outbox"], os.path.dirname(case.log)):
            for base, _dirs, files in os.walk(directory):
                for name in files:
                    with open(os.path.join(base, name), "rb") as handle:
                        data = handle.read()
                    self.assertNotIn(secret_env.encode(), data, name)
                    self.assertNotIn(secret_arg.encode(), data, name)
                    self.assertNotIn(b"--licence", data, name)


class UnsafeInputTests(Base):
    def refused(self, case, config=None, metadata=None, pattern=""):
        with self.assertRaisesRegex(Refused, pattern):
            build_events.run_build(config or case.config, metadata or case.metadata, case.command())
        self.assertEqual(case.ran(), [])
        self.assertEqual(case.entries(), [])

    def test_unsafe_configuration_is_refused_before_anything_is_written(self):
        case = self.case()
        log_dir = os.path.dirname(case.log)
        inside_key = os.path.join(log_dir, "key.pem")
        outbox_key = os.path.join(case.config["outbox"], "key.pem")
        for path in (inside_key, outbox_key):
            with open(path, "w") as handle:
                handle.write("key\n")
        link_target = os.path.join(case.root, "elsewhere")
        os.makedirs(link_target)
        os.symlink(link_target, os.path.join(case.root, "linked"))
        os.symlink(case.config["key"], os.path.join(case.root, "key-link.pem"))
        cases = {
            "relative log": (dict(case.config, log="ledger/stack.jsonl"), "absolute, normalized"),
            "dotted outbox": (dict(case.config, outbox=case.root + "/work/../outbox"), "absolute, normalized"),
            "shared head file": (dict(case.config, head=os.path.join(case.root, "head.json")), "outside the build-event schema"),
            "key in ledger dir": (dict(case.config, key=inside_key), "inside the ledger directory"),
            "key in outbox": (dict(case.config, key=outbox_key), "inside the build-event outbox"),
            "key is a link": (dict(case.config, key=os.path.join(case.root, "key-link.pem")), "not a regular file"),
            "missing key": (dict(case.config, key=os.path.join(case.root, "none.pem")), "cannot be inspected"),
            "outbox in ledger dir": (dict(case.config, outbox=os.path.join(log_dir, "box")), "must be disjoint"),
            "ledger in outbox": (dict(case.config, log=os.path.join(case.config["outbox"], "l.jsonl")), "must be disjoint"),
            "outbox through link": (dict(case.config, outbox=os.path.join(case.root, "linked", "box")), "symbolic link"),
            "zero timeout": (dict(case.config, timeout=0), "positive"),
            "required string": (dict(case.config, required="yes"), "true or false"),
        }
        for name, (config, pattern) in cases.items():
            with self.subTest(name):
                self.refused(case, config=config, pattern=pattern)
                self.assertEqual(os.listdir(link_target), [])
                self.assertFalse(os.path.exists(case.op_dir()))

    def test_unsafe_metadata_is_refused(self):
        case = self.case()
        meta = case.metadata
        cases = {
            "parent operation": (dict(meta, operation="../escape"), "operation must match"),
            "nested operation": (dict(meta, operation="a/b"), "operation must match"),
            "dots in operation": (dict(meta, operation="a..b"), "operation must match"),
            "environment field": (dict(meta, environment={"HOME": "/root"}), "outside the build-event schema"),
            "argv field": (dict(meta, argv=["eb", "--licence=x"]), "outside the build-event schema"),
            "licence in tool": (dict(meta, tool={"name": "eb", "licence": "x"}), "outside the build-event schema"),
            "source path": (dict(meta, sources=[{"name": "../x.tar.gz", "sha256": sha(b"x")}]), "plain file name"),
            "short hash": (dict(meta, patches=[{"name": "p.patch", "sha256": "abc"}]), "64 lower-case hex"),
            "log credentials": (dict(meta, logs=[{"uri": "https://user:pw@ci.example.org/log"}]), "without credentials"),
            "log token": (dict(meta, logs=[{"uri": "https://ci.example.org/log?token=abc"}]), "without credentials"),
            "relative location": (dict(meta, location="software/zlib"), "absolute, normalized"),
            "key material": (dict(meta, actor="-----BEGIN PRIVATE KEY-----"), "key material"),
            "control character": (dict(meta, module="zlib\n1.3"), "control character"),
            "ledger-only action": (dict(meta, action="remove"), "action must be one of"),
            "spaced target": (dict(meta, target={"cpu": "x86_64 zen4"}), "slash-separated"),
            "bad accession": (dict(meta, artifact={"accession": "md5:abc"}), "sha256:<64"),
            "wrong easyconfig hash": (dict(meta, easyconfig={"path": case.easyconfig, "sha256": "0" * 64}), "not the supplied"),
            "missing easyconfig": (dict(meta, easyconfig={"path": case.root + "/none.eb"}), "cannot be hashed"),
        }
        for name, (metadata, pattern) in cases.items():
            with self.subTest(name):
                self.refused(case, metadata=metadata, pattern=pattern)
                self.assertFalse(os.path.exists(os.path.join(case.op_dir(), "operation.json")))

    def test_symlinked_operation_directory_is_refused(self):
        case = self.case()
        elsewhere = os.path.join(case.root, "elsewhere")
        os.makedirs(elsewhere)
        os.symlink(elsewhere, case.op_dir())
        result = case.cli(command=case.command())
        self.assertEqual(result.returncode, build_events.REFUSED_EXIT)
        self.assertIn("not a plain directory", result.stderr)
        self.assertEqual(os.listdir(elsewhere), [])
        self.assertEqual(case.ran(), [])

    def test_a_call_off_the_main_thread_is_refused_before_started_is_appended(self):
        import threading

        case = self.case()
        errors = []
        worker = threading.Thread(target=lambda: errors.append(self._capture(case)))
        worker.start()
        worker.join()
        self.assertIsInstance(errors[0], Refused)
        self.assertEqual((case.entries(), case.ran()), ([], []))
        self.assertFalse(os.path.exists(case.op_dir()))

    @staticmethod
    def _capture(case):
        try:
            case.run_api()
        except Refused as exc:
            return exc
        return None

    def test_oversized_metadata_is_refused(self):
        case = self.case()
        with open(case.metadata_path, "w") as handle:
            json.dump(dict(case.metadata, actor="a" * (2 * 1024 * 1024)), handle)
        result = case.cli(command=case.command())
        self.assertEqual(result.returncode, build_events.REFUSED_EXIT)
        self.assertIn("larger than", result.stderr)
        self.assertEqual(case.ran(), [])

    def test_cli_needs_exactly_one_of_a_command_and_deliver(self):
        case = self.case()
        self.assertEqual(case.cli().returncode, build_events.REFUSED_EXIT)
        self.assertEqual(case.cli("--deliver", command=case.command()).returncode, build_events.REFUSED_EXIT)
        result = case.cli("--deliver")
        self.assertEqual(result.returncode, build_events.REFUSED_EXIT)
        self.assertIn("no recorded state", result.stderr)


class RealLedgerTests(Base):
    def setUp(self):
        super().setUp()
        self.bin = os.path.abspath(os.environ["EB_STACK_BIN"])
        keys = os.path.join(self.tmp, "keys")
        os.makedirs(keys)
        self.key = os.path.join(keys, "ledger.pem")
        self.pub = os.path.join(keys, "ledger.pub")
        subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", self.key], check=True, capture_output=True)
        subprocess.run(["openssl", "pkey", "-in", self.key, "-pubout", "-out", self.pub], check=True, capture_output=True)
        os.chmod(self.key, stat.S_IRUSR)

    def real(self, name="case", wrapper=None, **kwargs):
        eb_stack = self.bin
        if wrapper is not None:
            eb_stack = os.path.join(self.tmp, f"{name}-eb-stack")
            with open(eb_stack, "w") as handle:
                handle.write(textwrap.dedent(wrapper).format(bin=self.bin, count=eb_stack + ".count"))
            os.chmod(eb_stack, 0o755)
        return self.case(name, eb_stack=eb_stack, key=self.key, public_key=self.pub, timeout=60, **kwargs)

    def verify(self, case, record):
        head = os.path.join(case.op_dir(), "head.json")
        return subprocess.run([self.bin, "ledger", "verify", "--log", case.log, "--public-key", self.pub,
                               "--record", str(record), "--head", head], capture_output=True, text=True)

    def test_clean_run_is_verified_by_inclusion_proofs(self):
        case = self.real()
        result = case.cli(command=case.command())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(case.ran(), ["acked"])
        self.assertEqual(case.stages(), ["started", "finished"])
        self.assertTrue(all(event["receipt"]["verified"] for event in case.events()))
        for index in range(2):
            self.assertEqual(self.verify(case, index).returncode, 0)
        shown = subprocess.run([self.bin, "ledger", "show", "--log", case.log, "--json"], capture_output=True, text=True, check=True)
        self.assertEqual(len(json.loads(shown.stdout)), 2)

    def test_lost_ack_before_the_append_blocks_and_retries(self):
        case = self.real("before", wrapper="""\
            #!/bin/sh
            if [ "$2" = append ] && [ ! -e "{count}" ]; then : > "{count}"; echo "error: lost before append" >&2; exit 1; fi
            exec "{bin}" "$@"
        """)
        result = case.cli(command=case.command())
        self.assertEqual(result.returncode, build_events.PENDING_EXIT, result.stderr)
        self.assertEqual((case.entries(), case.ran()), ([], []))
        retry = case.cli(command=case.command())
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertFalse(case.events()[0]["receipt"]["replayed"])
        self.assertEqual(case.stages(), ["started", "finished"])

    def test_lost_ack_after_the_append_is_replayed_once(self):
        case = self.real("after", wrapper="""\
            #!/bin/sh
            if [ "$2" = append ] && [ ! -e "{count}" ]; then : > "{count}"; "{bin}" "$@" > /dev/null; exit 1; fi
            exec "{bin}" "$@"
        """)
        result = case.cli(command=case.command())
        self.assertEqual(result.returncode, build_events.PENDING_EXIT, result.stderr)
        self.assertEqual(case.stages(), ["started"])
        self.assertEqual(case.ran(), [])
        retry = case.cli(command=case.command())
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertTrue(case.events()[0]["receipt"]["replayed"])
        self.assertEqual(case.ran(), ["acked"])
        self.assertEqual(case.stages(), ["started", "finished"])
        case.check_unique(self)
        for index in range(2):
            self.assertEqual(self.verify(case, index).returncode, 0)

    def test_exact_replay_and_changed_payload_against_the_real_ledger(self):
        case = self.real()
        self.assertEqual(case.cli(command=case.command()).returncode, 0)
        with open(case.log, "rb") as handle:
            before = handle.read()
        data = outbox.load(case.op_dir())
        for event in data["events"]:
            event["status"] = "pending"
        with open(outbox.outbox_path(case.op_dir()), "w") as handle:
            json.dump(data, handle)
        self.assertEqual(case.cli("--deliver").returncode, 0)
        self.assertEqual([event["receipt"]["replayed"] for event in case.events()], [True, True])
        with open(case.log, "rb") as handle:
            self.assertEqual(handle.read(), before)
        case.config["outbox"] = os.path.join(case.root, "outbox-2")
        case.metadata["location"] = "/cvmfs/example/other"
        case.write()
        result = case.cli(command=case.command())
        self.assertEqual(result.returncode, build_events.PENDING_EXIT)
        self.assertIn("different content", outbox.pending({"events": case.events()})[0]["error"])
        self.assertEqual(case.ran(), ["acked"])
        with open(case.log, "rb") as handle:
            self.assertEqual(handle.read(), before)

    def test_independent_writers_share_one_ledger(self):
        case = self.real()
        writers = []
        for number in range(6):
            operation = f"writer-{number}"
            metadata = dict(case.metadata, operation=operation)
            path = os.path.join(case.root, f"metadata-{number}.json")
            with open(path, "w") as handle:
                json.dump(metadata, handle)
            argv = [*CLI, "--config", case.config_path,
                    "--metadata", path, "--", *case.command(operation=operation)]
            writers.append(subprocess.Popen(argv, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        for writer in writers:
            _out, err = writer.communicate(timeout=120)
            self.assertEqual(writer.returncode, 0, err)
        case.check_unique(self)
        self.assertEqual(len(case.entries()), 12)
        self.assertEqual(case.ran(), ["acked"] * 6)
        for number in range(6):
            self.assertEqual(case.stages(f"writer-{number}"), ["started", "finished"])
            for event in case.events(f"writer-{number}"):
                head = os.path.join(case.op_dir(f"writer-{number}"), "head.json")
                verify = subprocess.run([self.bin, "ledger", "verify", "--log", case.log, "--public-key", self.pub,
                                         "--record", str(event["receipt"]["index"]), "--head", head],
                                        capture_output=True, text=True)
                self.assertEqual(verify.returncode, 0, verify.stderr)


if __name__ == "__main__":
    unittest.main()

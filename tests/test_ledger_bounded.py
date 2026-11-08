"""The ledger CLI boundary bounds output storage and outlives no helper process.

Helpers here are real processes: group leaders that fork writing descendants,
writers that leave the group, and streams far larger than the output limit.
"""

import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest

from eb_staged_rebuild.ledger_adapter import _OUTPUT_LIMIT, EbStackLedger, _bounded, load_config
from eb_staged_rebuild.outbox import PublisherError
from tests.test_command_group_exit import group_members, holders

REPO = Path(__file__).resolve().parent.parent
FAKE = REPO / "tests" / "fake_eb_stack.py"
NOISE = 64 * 1024 * 1024

def alive(pid):
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state not in ("Z", "X")

def wait_for(predicate, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()

def stored_bytes(pid):
    """Bytes held in regular files behind the descriptors 1 and 2 of ``pid``."""
    total = 0
    for fd in (1, 2):
        try:
            info = os.stat(f"/proc/{pid}/fd/{fd}")
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            total += info.st_size
    return total

class BoundedOutputTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(self.reap)

    def reap(self):
        for path in self.root.glob("*.pid"):
            pid = int(path.read_text())
            for kill in (os.killpg, os.kill):
                try:
                    kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        subprocess.run(["rm", "-rf", str(self.root)], check=False)

    def helper(self, body):
        """A python helper whose prologue records its pid and defines ``noise``."""
        prologue = textwrap.dedent(f"""\
            import os, signal, sys, time
            from pathlib import Path
            ROOT = Path({str(self.root)!r})
            def mark(name):
                (ROOT / (name + ".pid")).write_text(str(os.getpid()))
            def noise(streams, total, rest=60, pause=0):
                chunk = b"n" * 65536
                written = 0
                while written < total:
                    for fd in streams:
                        os.write(fd, chunk)
                    written += len(chunk)
                    (ROOT / "written").write_text(str(written))
                    time.sleep(pause)
                time.sleep(rest)
            mark("leader")
        """)
        return [sys.executable, "-c", prologue + textwrap.dedent(body)]

    def pid(self, name):
        path = self.root / f"{name}.pid"
        self.assertTrue(wait_for(path.exists, 10), f"{name} never started")
        return int(path.read_text())

    def test_continuous_output_is_never_stored(self):
        argv = self.helper("noise((1, 2), %d)\n" % NOISE)
        peak = [0]
        done = threading.Event()

        def sample():
            while not done.is_set():
                path = self.root / "leader.pid"
                if path.exists():
                    peak[0] = max(peak[0], stored_bytes(int(path.read_text())))
                time.sleep(0.005)

        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        try:
            with self.assertRaisesRegex(PublisherError, "timed out after 3s"):
                _bounded(argv + ["ledger", "append"], 3)
        finally:
            done.set()
            sampler.join()
        written = int((self.root / "written").read_text())
        self.assertGreaterEqual(written, 16 * _OUTPUT_LIMIT, "the helper must outrun the limit")
        self.assertLessEqual(peak[0], 2 * _OUTPUT_LIMIT, f"{peak[0]} bytes stored after {written} written")
        self.assertEqual(group_members(self.pid("leader")), [])

    def test_oversized_streams_keep_the_leading_limit_and_the_exit_code(self):
        argv = self.helper("""\
            os.write(1, b"head\\n")
            os.write(2, b"warn\\n")
            noise((1, 2), 8 * 1024 * 1024, rest=0)
            sys.exit(3)
        """)
        code, out, err = _bounded(argv, 30)
        self.assertEqual(code, 3)
        self.assertEqual(len(out), _OUTPUT_LIMIT)
        self.assertEqual(len(err), _OUTPUT_LIMIT)
        self.assertTrue(out.startswith("head\nnnn"))
        self.assertTrue(err.startswith("warn\nnnn"))

    def test_writing_descendants_are_stopped_when_the_leader_exits(self):
        output = self.root / "output"
        argv = self.helper(f"""\
            fd = os.open({str(output)!r}, os.O_WRONLY | os.O_CREAT, 0o600)
            for number in range(4):
                if os.fork() == 0:
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    mark(f"worker{{number}}")
                    while True:
                        os.write(1, b"out\\n")
                        os.write(2, b"err\\n")
                        os.write(fd, b"file\\n")
                        time.sleep(0.01)
            while len(list(ROOT.glob("worker*.pid"))) < 4:
                time.sleep(0.01)
            print("done", flush=True)
        """)
        code, out, _err = _bounded(argv, 30)
        self.assertEqual(code, 0)
        self.assertIn("done", out)
        self.assertEqual(group_members(self.pid("leader")), [])
        self.assertEqual(holders(output), [])
        for number in range(4):
            self.assertFalse(alive(self.pid(f"worker{number}")))

    def test_a_writer_that_left_the_group_cannot_hold_the_call_or_outlive_its_pipe(self):
        argv = self.helper(f"""\
            if os.fork() == 0:
                os.setsid()
                mark("escaped")
                noise((1, 2), {NOISE}, rest=120, pause=0.002)
            while not (ROOT / "escaped.pid").exists():
                time.sleep(0.01)
            print("done", flush=True)
        """)
        started = time.monotonic()
        code, _out, _err = _bounded(argv, 30)
        self.assertEqual(code, 0)
        self.assertLess(time.monotonic() - started, 15)
        escaped = self.pid("escaped")
        self.assertTrue(wait_for(lambda: not alive(escaped), 10), "the escaped writer kept its pipe")

    def test_timeout_returns_after_every_group_member_has_exited(self):
        output = self.root / "output"
        argv = self.helper(f"""\
            fd = os.open({str(output)!r}, os.O_WRONLY | os.O_CREAT, 0o600)
            for number in range(16):
                if os.fork() == 0:
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    while True:
                        os.write(fd, b"x")
                        time.sleep(0.01)
            time.sleep(3600)
        """)
        with self.assertRaisesRegex(PublisherError, "timed out after 1s"):
            _bounded(argv + ["ledger", "append"], 1)
        self.assertEqual(group_members(self.pid("leader")), [])
        self.assertEqual(holders(output), [])

    def test_interrupted_call_returns_after_every_group_member_has_exited(self):
        output = self.root / "output"
        helper = self.helper(f"""\
            fd = os.open({str(output)!r}, os.O_WRONLY | os.O_CREAT, 0o600)
            for number in range(8):
                if os.fork() == 0:
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    mark(f"worker{{number}}")
                    while True:
                        os.write(1, b"out\\n")
                        os.write(fd, b"x")
                        time.sleep(0.01)
            time.sleep(3600)
        """)
        code = textwrap.dedent(f"""\
            import json, os, signal, threading, time
            from pathlib import Path
            from eb_staged_rebuild.ledger_adapter import _bounded
            from tests.test_command_group_exit import group_members, holders
            root = Path({str(self.root)!r})
            def stop():
                while len(list(root.glob("worker*.pid"))) < 8:
                    time.sleep(0.01)
                os.kill(os.getpid(), signal.SIGINT)
            threading.Thread(target=stop, daemon=True).start()
            try:
                _bounded({helper!r}, 60)
                interrupted = False
            except KeyboardInterrupt:
                interrupted = True
            pgid = int((root / "leader.pid").read_text())
            print(json.dumps([interrupted, group_members(pgid), holders(Path({str(output)!r}))]))
        """)
        completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, cwd=REPO)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        interrupted, members, open_writers = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertTrue(interrupted)
        self.assertEqual(members, [])
        self.assertEqual(open_writers, [])

    def test_a_noisy_helper_cannot_outlive_a_killed_caller(self):
        helper = self.helper("noise((1, 2), %d, rest=120, pause=0.002)\n" % NOISE)
        code = f"from eb_staged_rebuild.ledger_adapter import _bounded\n_bounded({helper!r}, 120)\n"
        caller = subprocess.Popen([sys.executable, "-c", code], cwd=REPO)
        try:
            leader = self.pid("leader")
            self.assertTrue(wait_for(lambda: (self.root / "written").exists(), 10))
        finally:
            caller.kill()
            caller.wait()
        self.assertTrue(wait_for(lambda: not alive(leader), 10), "the helper kept writing after its caller died")

class ExactReceiptTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.root)], check=False)

    def ledger(self, stdout_bytes, receipt_first=True):
        """A ledger whose eb-stack pads its stdout to ``stdout_bytes`` and floods stderr.

        The receipt is the first line eb-stack 0.6 prints; ``receipt_first``
        False puts the padding before it, as a helper that chatters before
        the receipt would.
        """
        wrapper = self.root / "eb-stack"
        wrapper.write_text(textwrap.dedent(f"""\
            #!{sys.executable}
            import os, subprocess, sys
            done = subprocess.run([sys.executable, {str(FAKE)!r}, *sys.argv[1:]], capture_output=True)
            pad = {stdout_bytes} - len(done.stdout)
            padding = b"p" * (pad - 1) + b"\\n" if pad > 0 else b""
            body = done.stdout + padding if {receipt_first!r} else padding + done.stdout
            with open({str(self.root / "sizes")!r}, "a") as handle:
                handle.write(str(len(body)) + "\\n")
            for start in range(0, len(body), 4096):
                os.write(1, body[start:start + 4096])
            chunk = b"w" * 65536
            for _ in range(128):
                os.write(2, chunk)
            sys.exit(done.returncode)
        """))
        wrapper.chmod(0o755)
        config = {
            "eb_stack": str(wrapper), "log": str(self.root / "stack.jsonl"), "key": str(self.root / "unused.pem"),
            "head": str(self.root / "head.json"), "public_key": str(self.root / "unused.pub"),
            "actor": "bounded-test", "stack": "2026.06", "target": "x86_64/generic", "timeout": 30,
        }
        path = self.root / "ledger.json"
        path.write_text(json.dumps(config))
        record = self.root / "record.json"
        record.write_text(json.dumps({"actor": "bounded-test", "build": {"event_id": "exact-limit"}}))
        return EbStackLedger(load_config(str(path))), str(record)

    def sizes(self):
        return [int(line) for line in (self.root / "sizes").read_text().split()]

    def test_receipt_that_fills_the_output_limit_is_accepted(self):
        ledger, record = self.ledger(_OUTPUT_LIMIT)
        receipt = ledger.append(record)
        self.assertEqual((receipt["index"], receipt["head_size"], receipt["replayed"], receipt["verified"]), (0, 1, False, True))
        again = ledger.append(record)
        self.assertEqual((again["index"], again["id"], again["replayed"]), (0, receipt["id"], True))
        self.assertEqual(self.sizes()[0], _OUTPUT_LIMIT)
        self.assertEqual(self.sizes()[1], _OUTPUT_LIMIT)

    def test_receipt_beyond_the_output_limit_is_not_read(self):
        ledger, record = self.ledger(2 * _OUTPUT_LIMIT, receipt_first=False)
        with self.assertRaisesRegex(PublisherError, "printed no receipt"):
            ledger.append(record)
        self.assertEqual(self.sizes(), [2 * _OUTPUT_LIMIT])

if __name__ == "__main__":
    unittest.main()

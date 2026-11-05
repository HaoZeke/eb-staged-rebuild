import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.lockfile import FileLock


class CommandLifecycleTests(unittest.TestCase):
    def check_termination(self, sent_signal, ignore_term):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'leaf.py').write_text(
                'import signal, time\nfrom pathlib import Path\n'
                'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
                'signal.signal(signal.SIGINT, signal.SIG_IGN)\n'
                f'Path({str(root / "leaf-ready")!r}).touch()\n'
                'while True: time.sleep(0.05)\n'
            )
            (root / 'worker.py').write_text(
                'import json, os, signal, subprocess, sys, time\nfrom pathlib import Path\n'
                + ('signal.signal(signal.SIGTERM, signal.SIG_IGN)\n' if ignore_term else '')
                + f'root = Path({directory!r})\n'
                'leaf = subprocess.Popen([sys.executable, str(root / "leaf.py")])\n'
                'while not (root / "leaf-ready").exists(): time.sleep(0.01)\n'
                '(root / "pids.json").write_text(json.dumps([os.getpid(), leaf.pid]))\n'
                'while True: time.sleep(0.05)\n'
            )
            parent_code = (
                'import os, sys\nfrom eb_staged_rebuild.builders import run_command\n'
                'from eb_staged_rebuild.faults import FaultInjector\n'
                'from eb_staged_rebuild.lockfile import FileLock\n'
                f'lock = FileLock({str(root / "state")!r})\nlock.acquire()\n'
                'try:\n'
                f'    code = run_command([sys.executable, {str(root / "worker.py")!r}], '
                'os.environ.copy(), FaultInjector(), "child")\n'
                'finally:\n    lock.release()\n'
                'sys.exit(code)\n'
            )
            parent = subprocess.Popen([sys.executable, '-c', parent_code],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            descendants = []
            try:
                deadline = time.monotonic() + 10
                while not (root / 'pids.json').exists() and time.monotonic() < deadline:
                    self.assertIsNone(parent.poll(), 'command parent exited before readiness')
                    time.sleep(0.01)
                descendants = json.loads((root / 'pids.json').read_text())
                parent.send_signal(sent_signal)
                if ignore_term:
                    contender = FileLock(root / 'state')
                    try:
                        with self.assertRaises(Refused):
                            contender.acquire(timeout=0.2)
                    finally:
                        contender.release()
                self.assertEqual(parent.wait(timeout=10), 128 + sent_signal)
                for pid in descendants:
                    status = Path(f'/proc/{pid}/stat')
                    if status.exists():
                        state = status.read_text().rsplit(')', 1)[1].split()[0]
                        self.assertEqual(state, 'Z', f'command descendant {pid} remains active')
                contender = FileLock(root / 'state')
                contender.acquire(timeout=1)
                contender.release()
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait()
                if not descendants and (root / 'pids.json').exists():
                    descendants = json.loads((root / 'pids.json').read_text())
                for pid in descendants:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_sigterm_holds_publication_lock_until_command_group_stops(self):
        self.check_termination(signal.SIGTERM, ignore_term=True)

    def test_interrupt_stops_descendants_when_the_direct_child_exits_first(self):
        self.check_termination(signal.SIGINT, ignore_term=False)


if __name__ == '__main__':
    unittest.main()

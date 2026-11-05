"""run_command returns only after every process in the command group has exited."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest

from eb_staged_rebuild.builders import run_command
from eb_staged_rebuild.faults import FaultInjector

def group_members(pgid):
    members = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            fields = Path(f"/proc/{name}/stat").read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if int(fields[2]) == pgid and fields[0] not in ("Z", "X"):
            members.append(int(name))
    return members

def holders(path):
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            for fd in os.listdir(f"/proc/{name}/fd"):
                if os.readlink(f"/proc/{name}/fd/{fd}") == str(path):
                    found.append(int(name))
        except OSError:
            continue
    return found

class CommandGroupExitTests(unittest.TestCase):
    def check(self, interrupt):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            leader = root / "leader"
            ready = root / "ready"
            worker = (
                "import os, signal, time\nfrom pathlib import Path\n"
                f"Path({str(leader)!r}).write_text(str(os.getpid()))\n"
                f"fd = os.open({str(output)!r}, os.O_WRONLY | os.O_CREAT, 0o600)\n"
                "for _ in range(48):\n"
                "    if os.fork() == 0:\n"
                "        signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "        while True:\n"
                "            pass\n"
                f"Path({str(ready)!r}).touch()\n"
                + ("while True: time.sleep(0.05)\n" if interrupt else "time.sleep(0.2)\n")
            )
            if interrupt:
                code = (
                    "import json, os, signal, sys, threading, time\nfrom pathlib import Path\n"
                    "from eb_staged_rebuild.builders import run_command\n"
                    "from eb_staged_rebuild.faults import FaultInjector\n"
                    "def stop():\n"
                    f"    while not Path({str(ready)!r}).exists(): time.sleep(0.01)\n"
                    "    os.kill(os.getpid(), signal.SIGTERM)\n"
                    "threading.Thread(target=stop, daemon=True).start()\n"
                    f"rc = run_command([sys.executable, '-c', {worker!r}], os.environ.copy(), FaultInjector(), 'x')\n"
                    f"pgid = int(Path({str(leader)!r}).read_text())\n"
                    "from tests.test_command_group_exit import group_members, holders\n"
                    f"print(json.dumps([rc, group_members(pgid), holders(Path({str(output)!r}))]))\n"
                )
                completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                           timeout=60, cwd=Path(__file__).resolve().parent.parent)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                rc, members, open_writers = json.loads(completed.stdout.strip().splitlines()[-1])
                self.assertEqual(rc, 128 + signal.SIGTERM)
            else:
                rc = run_command([sys.executable, "-c", worker], os.environ.copy(), FaultInjector(), "x")
                pgid = int(leader.read_text())
                members, open_writers = group_members(pgid), holders(output)
                self.assertEqual(rc, 0)
            self.assertEqual(members, [])
            self.assertEqual(open_writers, [])

    def test_normal_exit_returns_after_stray_workers_have_exited(self):
        for attempt in range(3):
            with self.subTest(attempt=attempt):
                self.check(interrupt=False)

    def test_interrupted_command_returns_after_every_worker_has_exited(self):
        self.check(interrupt=True)

if __name__ == "__main__":
    unittest.main()

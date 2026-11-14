"""test_build_events' in-process cases through the command line.

The crash and unsafe-input tests of test_build_events call build_events in
process. Here the same test bodies drive the build-events command, against
TOOL_BIN or, without it, the Python module:

- a crash at a step pauses the command there (EB_STAGED_REBUILD_PAUSE_AT)
  and kills it with SIGKILL, where the in-process test raises InjectedCrash;
- a refusal is the command's exit code 2 and its message on stderr.

The step list comes from the Python fault recorder, so a step the command
never reaches fails the test.
"""

import json
import os
import signal
import subprocess
import time
import unittest

from eb_staged_rebuild import build_events
from eb_staged_rebuild.faults import InjectedCrash
import tests.test_build_events as tbe


class Ran:
    def __init__(self, done):
        self.returncode = done.returncode
        self.message = done.stderr


class CliCase(tbe.Case):
    def run_api(self, code=0, faults=None):
        if faults is not None and faults.fail_at is None:
            # The recorder that lists the steps: the Python implementation.
            return super().run_api(code, faults)
        if faults is None:
            return Ran(self.cli(command=self.command(code)))
        ready = os.path.join(self.root, "paused")
        if os.path.exists(ready):
            os.unlink(ready)
        env = dict(os.environ, EB_STAGED_REBUILD_PAUSE_AT=faults.fail_at, EB_STAGED_REBUILD_PAUSE_READY=ready,
                   EB_STAGED_REBUILD_PAUSE_SECONDS="60")
        child = subprocess.Popen([*tbe.CLI, "--config", self.config_path, "--metadata", self.metadata_path,
                                  "--", *self.command(code)], cwd=tbe.REPO, env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 60
        while not os.path.exists(ready) and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if not os.path.exists(ready):
            child.kill()
            _out, err = child.communicate(timeout=60)
            raise AssertionError(f"the command never reached {faults.fail_at}: {err}")
        child.send_signal(signal.SIGKILL)
        child.communicate(timeout=60)
        raise InjectedCrash(faults.fail_at)


class CliBase(tbe.Base):
    def case(self, name="case", **kwargs):
        root = os.path.join(self.tmp, name)
        os.makedirs(root)
        return CliCase(root, **kwargs)


class CrashCliTests(CliBase):
    test_crash_before_and_after_the_append_is_retried_with_the_same_bytes = (
        tbe.AcknowledgementTests.test_crash_before_and_after_the_append_is_retried_with_the_same_bytes)
    test_crash_at_every_step_converges_without_duplicates_or_early_commands = (
        tbe.AcknowledgementTests.test_crash_at_every_step_converges_without_duplicates_or_early_commands)


class UnsafeInputCliTests(CliBase):
    def refused(self, case, config=None, metadata=None, pattern=""):
        config_path = os.path.join(case.root, "refused-config.json")
        metadata_path = os.path.join(case.root, "refused-metadata.json")
        with open(config_path, "w") as handle:
            json.dump(config or case.config, handle)
        with open(metadata_path, "w") as handle:
            json.dump(metadata or case.metadata, handle)
        done = subprocess.run([*tbe.CLI, "--config", config_path, "--metadata", metadata_path, "--", *case.command()],
                              cwd=tbe.REPO, capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, build_events.REFUSED_EXIT, done.stderr)
        self.assertRegex(done.stderr, pattern)
        self.assertEqual(case.ran(), [])
        self.assertEqual(case.entries(), [])

    test_unsafe_configuration_is_refused_before_anything_is_written = (
        tbe.UnsafeInputTests.test_unsafe_configuration_is_refused_before_anything_is_written)
    test_unsafe_metadata_is_refused = tbe.UnsafeInputTests.test_unsafe_metadata_is_refused


if __name__ == "__main__":
    unittest.main()

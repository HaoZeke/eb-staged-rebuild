"""SIGKILL during an ordinary-rename fallback, then resume or rollback.

Injected exceptions still run Python cleanup. These tests kill the process
after the first fallback rename and before the next one.
"""

import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

from eb_staged_rebuild.engine import resume, rollback
from eb_staged_rebuild.paths import lstat_kind
from tests.support import PROBES, classic_install, installpath_builder


class HardKillTests(unittest.TestCase):
    def test_sigkill_during_publish_fallback_resumes_once(self):
        with tempfile.TemporaryDirectory() as root:
            software = os.path.join(root, "software")
            modules = os.path.join(root, "modules")
            state = os.path.join(root, "state")
            os.makedirs(software)
            os.makedirs(modules)
            install, _module, _text = classic_install(
                software, modules, "demo/1", "v0\n", "all/demo/1.lua"
            )
            ready = os.path.join(root, "ready")
            builds = os.path.join(root, "builds")
            script = textwrap.dedent(
                f"""
                import os
                from eb_staged_rebuild.engine import Spec, run
                from eb_staged_rebuild.faults import FaultInjector
                from tests.support import PROBES, installpath_builder
                def builder(ctx):
                    path = {builds!r}
                    count = int(open(path).read()) if os.path.exists(path) else 0
                    open(path, "w").write(str(count + 1))
                    return installpath_builder([
                        {{"rel": "demo/1", "payload": "v1\\n", "module_rel": "all/demo/1.lua"}}
                    ])(ctx)
                run(Spec(
                    software={software!r},
                    modules={modules!r},
                    state_root={state!r},
                    eb_args=["demo-1.eb"],
                    backend="installpath",
                    module_rels=["demo/1"],
                    builder=builder,
                    faults=FaultInjector(),
                    run_id="run1",
                    probes=PROBES,
                ))
                """
            )
            env = os.environ.copy()
            env["EB_STAGED_REBUILD_PAUSE_AT"] = "swap:all/demo/1.lua.fallback.staged-to-live"
            env["EB_STAGED_REBUILD_PAUSE_READY"] = ready
            env["EB_STAGED_REBUILD_PAUSE_SECONDS"] = "30"
            child = subprocess.Popen(
                [sys.executable, "-c", script],
                env=env,
                cwd=os.path.dirname(os.path.dirname(__file__)),
            )
            try:
                deadline = time.monotonic() + 20
                while not os.path.exists(ready) and time.monotonic() < deadline:
                    self.assertIsNone(child.poll(), "publish exited before the fallback pause")
                    time.sleep(0.05)
                self.assertTrue(os.path.exists(ready), "pause was not reached")
                os.kill(child.pid, signal.SIGKILL)
                self.assertEqual(child.wait(timeout=10), -signal.SIGKILL)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
            self.assertNotEqual(lstat_kind(install), "dir")
            builder = installpath_builder(
                [{"rel": "demo/1", "payload": "v1\n", "module_rel": "all/demo/1.lua"}]
            )
            result = resume(state, "run1", builder=builder)
            self.assertEqual(result.returncode, 0, result.message)
            with open(os.path.join(install, "PAYLOAD"), encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "v1\n")
            again = resume(state, "run1", builder=builder)
            self.assertEqual(again.returncode, 0, again.message)
            self.assertEqual(open(builds, encoding="utf-8").read(), "1")

    def test_sigkill_during_rollback_fallback_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            software = os.path.join(root, "software")
            modules = os.path.join(root, "modules")
            state = os.path.join(root, "state")
            os.makedirs(software)
            os.makedirs(modules)
            install, _module, _text = classic_install(
                software, modules, "demo/1", "v0\n", "all/demo/1.lua"
            )
            from eb_staged_rebuild.engine import Spec, run
            from eb_staged_rebuild.faults import FaultInjector

            published = run(Spec(
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
            ))
            self.assertEqual(published.returncode, 0, published.message)
            ready = os.path.join(root, "ready")
            script = textwrap.dedent(
                f"""
                from eb_staged_rebuild.engine import rollback
                rollback({state!r}, "run1")
                """
            )
            env = os.environ.copy()
            env["EB_STAGED_REBUILD_PAUSE_AT"] = "rollback.swap:all/demo/1.lua.fallback.staged-to-live"
            env["EB_STAGED_REBUILD_PAUSE_READY"] = ready
            env["EB_STAGED_REBUILD_PAUSE_SECONDS"] = "30"
            child = subprocess.Popen(
                [sys.executable, "-c", script],
                env=env,
                cwd=os.path.dirname(os.path.dirname(__file__)),
            )
            try:
                deadline = time.monotonic() + 20
                while not os.path.exists(ready) and time.monotonic() < deadline:
                    self.assertIsNone(child.poll(), "rollback exited before the fallback pause")
                    time.sleep(0.05)
                self.assertTrue(os.path.exists(ready), "rollback pause was not reached")
                os.kill(child.pid, signal.SIGKILL)
                self.assertEqual(child.wait(timeout=10), -signal.SIGKILL)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
            result = rollback(state, "run1")
            self.assertEqual(result.returncode, 0, result.message)
            with open(os.path.join(install, "PAYLOAD"), encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "v0\n")
            self.assertEqual(rollback(state, "run1").returncode, 0)


if __name__ == "__main__":
    unittest.main()

import json
import os
import subprocess
import sys
import tempfile
import unittest

from eb_staged_rebuild.lockfile import FileLock
from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.probe import probe_all, probe_rename_exchange


class ProbeTests(unittest.TestCase):
    def test_rename_exchange_reports_a_result_for_this_directory(self):
        with tempfile.TemporaryDirectory() as root:
            info = probe_rename_exchange(root)
        self.assertIn("ok", info)
        self.assertIn("fstype", info)
        self.assertTrue(info["ok"] or info["errno"] is not None or info["error"])

    def test_probe_all_has_the_open_questions(self):
        with tempfile.TemporaryDirectory() as root:
            report = probe_all(root, eb="eb-not-installed-for-this-test")
        self.assertIn("bwrap", report)
        self.assertIn("user_namespaces", report)
        self.assertIn("userns_sandbox", report)
        self.assertEqual(report["flock"]["cross_node"], "unmeasured")
        self.assertFalse(report["easybuild"]["found"])


class LockTests(unittest.TestCase):
    def test_second_lock_does_not_wait_forever(self):
        with tempfile.TemporaryDirectory() as root:
            first = FileLock(root)
            first.acquire(0)
            second = FileLock(root)
            with self.assertRaises(Refused):
                second.acquire(0)
            first.release()
            second.acquire(0)
            second.release()


class CliTests(unittest.TestCase):
    def test_dry_run_and_probe_and_resolve(self):
        with tempfile.TemporaryDirectory() as root:
            software = os.path.join(root, "software")
            modules = os.path.join(root, "modules")
            os.makedirs(os.path.join(software, "demo", "1", "easybuild"))
            open(os.path.join(software, "demo", "1", "PAYLOAD"), "w", encoding="utf-8").write("v0\n")
            open(os.path.join(software, "demo", "1", "easybuild", "demo-1.eb"), "w", encoding="utf-8").write(
                "name = 'demo'\n"
            )
            module = os.path.join(modules, "all", "demo")
            os.makedirs(module)
            module_file = os.path.join(module, "1.lua")
            install = os.path.join(software, "demo", "1")
            open(module_file, "w", encoding="utf-8").write(
                f'local root = "{install}"\nsetenv("EBROOTDEMO", root)\n'
            )
            env = dict(os.environ)
            cmd = [
                sys.executable,
                "-m",
                "eb_staged_rebuild",
                "run",
                "--software",
                software,
                "--modules",
                modules,
                "--state-root",
                os.path.join(root, "state"),
                "--dry-run",
                "--module",
                "demo/1",
                "--",
                "demo-1.eb",
            ]
            completed = subprocess.run(cmd, cwd=os.path.dirname(os.path.dirname(__file__)) or ".", env=env, capture_output=True, text=True)
            # The package lives at the repo root. unittest's cwd is the repo when
            # launched from there; set PYTHONPATH explicitly.
            env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            completed = subprocess.run(cmd, env=env, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            self.assertIn("dry-run", completed.stdout)
            self.assertFalse(os.path.isdir(os.path.join(root, "state", "runs")))
            self.assertIn("v0\n", open(os.path.join(install, "PAYLOAD"), encoding="utf-8").read())

            probe = subprocess.run(
                [sys.executable, "-m", "eb_staged_rebuild", "probe", "--path", root, "--json", "--eb", "missing-eb"],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(probe.returncode, 0, probe.stderr)
            report = json.loads(probe.stdout)
            self.assertIn("rename_exchange", report)
            self.assertEqual(report["flock"]["cross_node"], "unmeasured")

            resolved = subprocess.run(
                [sys.executable, "-m", "eb_staged_rebuild", "resolve", module_file],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(resolved.returncode, 0, resolved.stderr)
            body = json.loads(resolved.stdout)
            self.assertEqual(body["root"], install)
            self.assertTrue(body["complete"])


if __name__ == "__main__":
    unittest.main()

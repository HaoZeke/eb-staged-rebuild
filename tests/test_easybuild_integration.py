"""Rebuild a real EasyBuild 5.4 easyconfig without emptying the live prefix.

Skipped unless EB_STAGED_REBUILD_INTEGRATION=1. When that variable is set, a
missing ``eb`` or Lmod is a failure, not a skip.

The easyconfig is a local Binary install of one file. EasyBuild 5.4.2 writes
``local root = "<installdir>"`` into the Lua module. This test installs that
once with ``eb``, then publishes a second build through eb-staged-rebuild.
"""

import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
import unittest

from eb_staged_rebuild.engine import Spec, run
from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.modulefile import parse_root
from eb_staged_rebuild.paths import lstat_kind


def _sha256(data):
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _easyconfig(path, digest):
    text = f"""\
easyblock = 'Binary'
name = 'stagedemo'
version = '1'
homepage = 'https://easybuild.io'
description = "Local file copy used to exercise a staged rebuild."
toolchain = SYSTEM
sources = ['stagedemo-1.txt']
checksums = ['{digest}']
sanity_check_paths = {{'files': ['stagedemo-1.txt'], 'dirs': []}}
moduleclass = 'tools'
"""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class Watcher(threading.Thread):
    def __init__(self, path, expected):
        super().__init__(daemon=True)
        self.path = path
        self.expected = set(expected)
        self.stop = threading.Event()
        self.samples = 0
        self.bad = 0
        self.notes = []

    def run(self):
        while not self.stop.is_set():
            self.samples += 1
            kind = lstat_kind(self.path)
            if kind != "file":
                self.bad += 1
                if len(self.notes) < 5:
                    self.notes.append(f"sample {self.samples}: {kind}")
            else:
                with open(self.path, encoding="utf-8") as handle:
                    data = handle.read()
                if data not in self.expected:
                    self.bad += 1
                    if len(self.notes) < 5:
                        self.notes.append(f"sample {self.samples}: {data!r}")
            self.stop.wait(0.005)


@unittest.skipUnless(
    os.environ.get("EB_STAGED_REBUILD_INTEGRATION") == "1",
    "set EB_STAGED_REBUILD_INTEGRATION=1 to rebuild a real easyconfig",
)
class EasyBuildIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        lmod = os.environ.get("LMOD_CMD")
        if not lmod and os.path.isfile("/usr/share/lmod/lmod/libexec/lmod"):
            lmod = "/usr/share/lmod/lmod/libexec/lmod"
            os.environ["LMOD_CMD"] = lmod
            os.environ["PATH"] = "/usr/share/lmod/lmod/libexec:" + os.environ.get("PATH", "")
        if shutil.which("eb") is None or not lmod or not os.path.isfile(lmod):
            raise AssertionError(
                "EB_STAGED_REBUILD_INTEGRATION=1 but eb or Lmod is missing "
                f"(eb={shutil.which('eb')}, LMOD_CMD={lmod})"
            )
        version = subprocess.run(["eb", "--version"], capture_output=True, text=True, check=False)
        text = (version.stdout or "") + (version.stderr or "")
        if version.returncode != 0 or "EasyBuild" not in text:
            raise AssertionError(f"eb --version failed: {text}")
        cls.eb_version_text = text.strip()

    def _install_v1(self, work):
        src = os.path.join(work, "src")
        os.makedirs(src)
        payload = "payload-v1\n"
        with open(os.path.join(src, "stagedemo-1.txt"), "w", encoding="utf-8") as handle:
            handle.write(payload)
        ec = os.path.join(work, "stagedemo-1.eb")
        _easyconfig(ec, _sha256(payload))
        prefix = os.path.join(work, "prefix")
        completed = subprocess.run(
            [
                "eb",
                ec,
                f"--sourcepath={src}",
                f"--prefix={prefix}",
                "--force",
                "--module-syntax=Lua",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            self.fail(completed.stdout + "\n" + completed.stderr)
        live = os.path.join(prefix, "software", "stagedemo", "1")
        module = os.path.join(prefix, "modules", "all", "stagedemo", "1.lua")
        self.assertTrue(os.path.isfile(os.path.join(live, "stagedemo-1.txt")))
        self.assertTrue(os.path.isfile(module))
        return prefix, src, ec, live, module

    def _retarget(self, src, ec, payload):
        with open(os.path.join(src, "stagedemo-1.txt"), "w", encoding="utf-8") as handle:
            handle.write(payload)
        _easyconfig(ec, _sha256(payload))

    def _rebuild(self, prefix, ec, src, backend):
        spec = Spec(
            software=os.path.join(prefix, "software"),
            modules=os.path.join(prefix, "modules"),
            state_root=os.path.join(prefix, ".eb-staged-rebuild"),
            eb_args=[ec, f"--sourcepath={src}", "--module-syntax=Lua"],
            backend=backend,
            module_rels=["stagedemo/1"],
            eb_executable="eb",
        )
        return run(spec)

    def _assert_switched(self, live, module, inode, watcher, generation_payload):
        self.stop_watcher(watcher)
        self.assertEqual(watcher.bad, 0, watcher.notes)
        self.assertGreater(watcher.samples, 0)
        self.assertNotEqual(os.lstat(live).st_ino, inode)
        with open(os.path.join(live, "stagedemo-1.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), generation_payload)
        with open(module, encoding="utf-8") as handle:
            text = handle.read()
        _syntax, root = parse_root(text)
        self.assertEqual(lstat_kind(root), "dir")
        self.assertEqual(os.path.realpath(root), os.path.realpath(live))
        with open(os.path.join(root, "stagedemo-1.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), generation_payload)
        print(
            f"integration {generation_payload.strip()} samples={watcher.samples} bad={watcher.bad}",
            flush=True,
        )

    @staticmethod
    def stop_watcher(watcher):
        watcher.stop.set()
        watcher.join(timeout=5)

    def test_installpath_backend_keeps_the_live_file(self):
        with tempfile.TemporaryDirectory() as work:
            prefix, src, ec, live, module = self._install_v1(work)
            inode = os.lstat(live).st_ino
            self._retarget(src, ec, "payload-v2\n")
            watcher = Watcher(
                os.path.join(live, "stagedemo-1.txt"),
                {"payload-v1\n", "payload-v2\n"},
            )
            watcher.start()
            try:
                result = self._rebuild(prefix, ec, src, "installpath")
            finally:
                self.stop_watcher(watcher)
            self.assertEqual(result.returncode, 0, result.message)
            self._assert_switched(live, module, inode, watcher, "payload-v2\n")

    def test_bwrap_backend_keeps_the_live_file(self):
        from eb_staged_rebuild.probe import probe_bwrap

        probe = probe_bwrap()
        if not probe.get("usable"):
            self.skipTest(f"bwrap is not usable here: {probe.get('reason')}")
        with tempfile.TemporaryDirectory() as work:
            prefix, src, ec, live, module = self._install_v1(work)
            inode = os.lstat(live).st_ino
            self._retarget(src, ec, "payload-v2\n")
            watcher = Watcher(
                os.path.join(live, "stagedemo-1.txt"),
                {"payload-v1\n", "payload-v2\n"},
            )
            watcher.start()
            try:
                result = self._rebuild(prefix, ec, src, "bwrap")
            finally:
                self.stop_watcher(watcher)
            self.assertEqual(result.returncode, 0, result.message)
            self._assert_switched(live, module, inode, watcher, "payload-v2\n")
            self.assertFalse(os.path.islink(live))

    def test_symlink_name_version_is_refused(self):
        with tempfile.TemporaryDirectory() as work:
            prefix, src, ec, live, module = self._install_v1(work)
            with open(module, encoding="utf-8") as handle:
                module_before = handle.read()
            moved = live + ".real"
            os.rename(live, moved)
            os.symlink(moved, live)
            with open(os.path.join(moved, "stagedemo-1.txt"), encoding="utf-8") as handle:
                before = handle.read()
            with self.assertRaises(Refused) as caught:
                self._rebuild(prefix, ec, src, "installpath")
            self.assertIn("symlink", str(caught.exception))
            self.assertTrue(os.path.islink(live))
            with open(os.path.join(moved, "stagedemo-1.txt"), encoding="utf-8") as handle:
                self.assertEqual(handle.read(), before)
            with open(module, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), module_before)


if __name__ == "__main__":
    unittest.main()

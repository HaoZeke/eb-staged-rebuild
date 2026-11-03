"""EasyBuild is replaced by tests/fake_eb.py. These tests exercise the real backends."""

import json
import os
import stat
import tempfile
import unittest
from types import SimpleNamespace

from eb_staged_rebuild.builders import userns_command
from eb_staged_rebuild.engine import Spec, run
from eb_staged_rebuild.modulefile import parse_root
from eb_staged_rebuild.probe import probe_userns_sandbox, stage_paths_from_bwrap_info
from eb_staged_rebuild.errors import Refused
from tests.support import PROBES, classic_install


FAKE_EB = os.path.join(os.path.dirname(__file__), "fake_eb.py")


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _probes(version, stage_keys):
    probes = json.loads(json.dumps(PROBES))
    probes["easybuild"]["framework_version"] = list(version) if version else None
    probes["easybuild"]["stage_path_keys"] = stage_keys
    return probes


class FakeEbTests(unittest.TestCase):
    def setUp(self):
        os.chmod(FAKE_EB, os.stat(FAKE_EB).st_mode | stat.S_IEXEC)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.software = os.path.join(self.root, "software")
        self.modules = os.path.join(self.root, "modules")
        self.state = os.path.join(self.root, "state")
        os.makedirs(self.software)
        os.makedirs(self.modules)
        self.install, self.module, self.v0 = classic_install(
            self.software, self.modules, "demo/1", "v0\n", "all/demo/1.lua"
        )
        self.inode = os.lstat(self.install).st_ino
        self.argv = os.path.join(self.root, "argv.log")
        self._env = dict(os.environ)
        os.environ["FAKE_EB_ARGV"] = self.argv
        os.environ["FAKE_EB_PAYLOAD"] = "v1\n"
        os.environ.pop("FAKE_EB_FAIL", None)
        os.environ.pop("FAKE_EB_KEYS", None)
        os.environ.pop("FAKE_EB_OVERLAY", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def _spec(self, backend, probes=None):
        return Spec(
            software=self.software,
            modules=self.modules,
            state_root=self.state,
            eb_args=[os.path.join(self.root, "demo-1.eb")],
            backend=backend,
            module_rels=["demo/1"],
            eb_executable=FAKE_EB,
            probes=probes or _probes((5, 4, 2), True),
            run_id="run1",
        )

    def _assert_live_untouched(self):
        self.assertEqual(os.lstat(self.install).st_ino, self.inode)
        self.assertEqual(_read(os.path.join(self.install, "PAYLOAD")), "v0\n")

    def _assert_switched(self):
        self.assertNotEqual(os.lstat(self.install).st_ino, self.inode)
        self.assertEqual(_read(os.path.join(self.install, "PAYLOAD")), "v1\n")
        _syntax, root = parse_root(_read(self.module))
        self.assertEqual(os.path.realpath(root), os.path.realpath(self.install))
        retired = os.path.join(os.path.dirname(self.install), ".1.run1")
        self.assertEqual(_read(os.path.join(retired, "PAYLOAD")), "v0\n")

    def test_installpath_swaps_the_staged_generation(self):
        result = run(self._spec("installpath"))
        self.assertEqual(result.returncode, 0, result.message)
        self._assert_switched()
        commands = [json.loads(line) for line in _read(self.argv).splitlines()]
        build = commands[-1]
        joined = " ".join(build)
        self.assertIn("--installpath-software=", joined)
        self.assertIn("--rebuild", build)
        self.assertNotIn("--remove-ghost-install-dirs", build)
        self.assertNotIn("--prefix", [arg.split("=", 1)[0] for arg in build])
        self.assertTrue(any(arg.startswith("--installpath-software=") and self.software not in arg for arg in build))

    def test_bwrap_swaps_the_staged_generation(self):
        result = run(self._spec("bwrap"))
        self.assertEqual(result.returncode, 0, result.message)
        self._assert_switched()
        self.assertFalse(os.path.islink(self.install))

    def test_userns_build_mounts_live_trees_read_only_then_swaps(self):
        probe = probe_userns_sandbox()
        if not probe.get("usable"):
            self.skipTest(probe.get("reason"))
        result = run(self._spec("userns"))
        self.assertEqual(result.returncode, 0, result.message)
        self._assert_switched()
        commands = [json.loads(line) for line in _read(self.argv).splitlines()]
        self.assertNotIn("--bwrap", commands[-1])
        self.assertTrue(
            any(argument.startswith("--installpath-software=") for argument in commands[-1])
        )

    def test_easybuild_53_layout_without_stage_keys(self):
        os.environ["FAKE_EB_KEYS"] = "legacy"
        result = run(self._spec("bwrap", _probes((5, 3, 1), False)))
        self.assertEqual(result.returncode, 0, result.message)
        self._assert_switched()
        _syntax, root = parse_root(_read(self.module))
        self.assertEqual(os.path.realpath(root), os.path.realpath(self.install))
        self.assertEqual(_read(os.path.join(root, "PAYLOAD")), "v1\n")

    def test_missing_stage_key_on_unknown_version_is_not_published(self):
        os.environ["FAKE_EB_KEYS"] = "legacy"
        result = run(self._spec("bwrap", _probes((5, 4, 1), None)))
        self.assertEqual(result.returncode, 2)
        self._assert_live_untouched()
        self.assertIn('local root = "', _read(self.module))
        self.assertIn(self.install, _read(self.module))

    def test_overlay_upper_is_not_published(self):
        os.environ["FAKE_EB_OVERLAY"] = "1"
        result = run(self._spec("bwrap"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("overlay", result.message)
        self._assert_live_untouched()
        self.assertEqual(_read(self.module), self.v0)

    def test_bwrap_refuses_when_probe_fails(self):
        probes = _probes((5, 4, 2), True)
        probes["bwrap"] = {"installed": True, "usable": False, "reason": "user namespaces disabled", "version": None, "path": "bwrap"}
        with self.assertRaises(Refused) as caught:
            run(self._spec("bwrap", probes))
        self.assertIn("namespaces", str(caught.exception))
        self._assert_live_untouched()
        self.assertFalse(os.path.isdir(os.path.join(self.state, "generations")))

    def test_auto_prefers_bwrap(self):
        result = run(self._spec("auto"))
        self.assertEqual(result.returncode, 0, result.message)
        commands = [json.loads(line) for line in _read(self.argv).splitlines()]
        self.assertIn("--bwrap", commands[-1])

    def test_auto_uses_installpath_when_no_sandbox_is_available(self):
        probes = _probes((5, 4, 2), True)
        probes["bwrap"]["usable"] = False
        probes["bwrap"]["reason"] = "disabled"
        probes["userns_sandbox"] = {
            "installed": True,
            "usable": False,
            "path": "unshare",
            "reason": "disabled",
        }
        result = run(self._spec("auto", probes))
        self.assertEqual(result.returncode, 0, result.message)
        commands = [json.loads(line) for line in _read(self.argv).splitlines()]
        self.assertNotIn("--bwrap", commands[-1])
        self.assertTrue(
            any(argument.startswith("--installpath-software=") for argument in commands[-1])
        )

    def test_userns_command_mounts_both_live_trees_read_only(self):
        context = SimpleNamespace(
            generation_root=os.path.join(self.root, "generation"),
            eb_executable=FAKE_EB,
            eb_args=[os.path.join(self.root, "demo-1.eb")],
            software=self.software,
            modules=self.modules,
            probes={
                "userns_sandbox": {
                    "path": "/usr/bin/unshare",
                }
            },
        )
        command, _env, _software, _modules = userns_command(context)
        self.assertEqual(command[0], "/usr/bin/unshare")
        self.assertEqual(command.count("--read-only"), 2)
        self.assertIn(self.software, command)
        self.assertIn(self.modules, command)
        self.assertIn("eb_staged_rebuild.userns_sandbox", command)


class BwrapInfoTests(unittest.TestCase):
    def test_legacy_versions_use_the_documented_layout(self):
        info = {"bwrap_installpath": "/stage", "bwrap_cmd": ["bwrap", "--bind", "a", "b"], "modules_to_install": ["demo/1"]}
        software, modules, how = stage_paths_from_bwrap_info(info, [5, 3, 0], "/stage")
        self.assertEqual(software, "/stage/software")
        self.assertEqual(modules, "/stage/modules")
        self.assertEqual(how, "easybuild-5.3-layout")

    def test_present_keys_win(self):
        info = {
            "bwrap_installpath_software": "/stage/software",
            "bwrap_installpath_modules": "/stage/modules",
            "bwrap_cmd": ["bwrap"],
        }
        software, modules, how = stage_paths_from_bwrap_info(info, [5, 4, 2], "/stage")
        self.assertEqual((software, modules, how), ("/stage/software", "/stage/modules", "bwrap_info"))

    def test_overlay_is_refused(self):
        info = {
            "bwrap_installpath_software": "/stage/software",
            "bwrap_installpath_modules": "/stage/modules",
            "bwrap_cmd": ["bwrap", "--overlay", "u", "w", "d"],
        }
        with self.assertRaises(Refused):
            stage_paths_from_bwrap_info(info, [5, 4, 2], "/stage")


if __name__ == "__main__":
    unittest.main()

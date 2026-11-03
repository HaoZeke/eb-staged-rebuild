"""Crash between every publish step. The live module stays on a complete generation."""

import os
import shutil
import tempfile
import unittest

from eb_staged_rebuild.engine import Spec, resume, rollback, run
from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.faults import FaultInjector, InjectedCrash
from eb_staged_rebuild.gc import collect
from eb_staged_rebuild.modulefile import parse_root
from eb_staged_rebuild.paths import abspath, lstat_kind
from tests.support import (
    PROBES,
    classic_install,
    generation_of,
    guard_calls,
    installpath_builder,
    lua,
    patch_os,
    restore_os,
)


def _tree(root):
    software = os.path.join(root, "software")
    modules = os.path.join(root, "modules")
    state = os.path.join(root, "state")
    os.makedirs(software)
    os.makedirs(modules)
    return software, modules, state


def _spec(software, modules, state, builder, run_id, rels, faults=None, clock=None):
    return Spec(
        software=software,
        modules=modules,
        state_root=state,
        eb_args=["demo-1.eb"],
        backend="installpath",
        module_rels=rels,
        builder=builder,
        faults=faults or FaultInjector(),
        run_id=run_id,
        probes=PROBES,
        clock=clock,
    )


class Clock:
    def __init__(self):
        self.now = 1_700_000_000.0

    def __call__(self):
        self.now += 10
        return self.now


class FaultTests(unittest.TestCase):
    def _one(self, root):
        software, modules, state = _tree(root)
        install, module, v0 = classic_install(
            software, modules, "demo/1", "v0\n", "all/demo/1.lua"
        )
        inode = os.lstat(install).st_ino
        builder = installpath_builder(
            [
                {
                    "rel": "demo/1",
                    "payload": "v1\n",
                    "module_rel": "all/demo/1.lua",
                    "link_rel": "tools/demo/1.lua",
                }
            ]
        )
        v1_root = generation_of(state, "run1", "demo/1")
        v1 = lua(v1_root)
        return software, modules, state, install, module, v0, v1, inode, builder

    def _check(self, module, v0, v1, install, inode):
        self.assertEqual(os.lstat(install).st_ino, inode)
        self.assertEqual(open(os.path.join(install, "PAYLOAD"), encoding="utf-8").read(), "v0\n")
        self.assertEqual(lstat_kind(module), "file")
        text = open(module, encoding="utf-8").read()
        self.assertIn(text, {v0, v1})
        _syntax, root = parse_root(text)
        self.assertEqual(lstat_kind(root), "dir")
        payload = open(os.path.join(root, "PAYLOAD"), encoding="utf-8").read()
        if text == v0:
            self.assertEqual(payload, "v0\n")
            self.assertEqual(abspath(root), abspath(install))
        else:
            self.assertEqual(payload, "v1\n")

    def test_success_then_every_fault_then_resume(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, install, module, v0, v1, inode, builder = self._one(root)
            spec = _spec(software, modules, state, builder, "record", ["demo/1"])
            # A recording run uses its own tree so the crash runs start clean.
            result = run(spec)
            self.assertEqual(result.returncode, 0, result.message)
            steps = list(spec.faults.reached)
            self.assertIn("publish.file:all/demo/1.lua.rename", steps)
            self.assertIn("publish.backup:all/demo/1.lua.rename", steps)
            self.assertIn("journal.module:all/demo/1.lua.rename", steps)
            self.assertIn("build.payload:demo/1", steps)
            self.assertIn("build.module:demo/1", steps)

        for step in steps:
            with self.subTest(step=step):
                with tempfile.TemporaryDirectory() as root:
                    software, modules, state, install, module, v0, v1, inode, builder = self._one(root)
                    guards = guard_calls([install])
                    originals = patch_os(guards)
                    try:
                        spec = _spec(
                            software,
                            modules,
                            state,
                            builder,
                            "run1",
                            ["demo/1"],
                            faults=FaultInjector(step),
                        )
                        with self.assertRaises(InjectedCrash) as caught:
                            run(spec)
                        self.assertEqual(caught.exception.step, step)
                        self._check(module, v0, v1, install, inode)
                        journal = os.path.join(state, "runs", "run1", "journal.json")
                        if os.path.isfile(journal):
                            resumed = resume(
                                state,
                                "run1",
                                builder=builder,
                                faults=FaultInjector(),
                            )
                            self.assertEqual(resumed.returncode, 0, resumed.message)
                            self._check(module, v0, v1, install, inode)
                            text = open(module, encoding="utf-8").read()
                            self.assertEqual(text, v1)
                    finally:
                        restore_os(originals)

    def test_resume_can_crash_and_still_converges(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, install, module, v0, v1, inode, builder = self._one(root)
            spec = _spec(
                software,
                modules,
                state,
                builder,
                "run1",
                ["demo/1"],
                faults=FaultInjector("publish.file:all/demo/1.lua.rename"),
            )
            with self.assertRaises(InjectedCrash):
                run(spec)
            self._check(module, v0, v1, install, inode)
            with self.assertRaises(InjectedCrash):
                resume(
                    state,
                    "run1",
                    builder=builder,
                    faults=FaultInjector("journal.complete.rename"),
                )
            self._check(module, v0, v1, install, inode)
            resumed = resume(state, "run1", builder=builder, faults=FaultInjector())
            self.assertEqual(resumed.returncode, 0)
            self.assertEqual(open(module, encoding="utf-8").read(), v1)
            self.assertEqual(os.lstat(install).st_ino, inode)

    def test_two_modules_stay_complete_between_publishes(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state = _tree(root)
            lib_install, lib_module, lib_v0 = classic_install(
                software, modules, "lib/1", "lib-v0\n", "all/lib/1.lua", name="lib"
            )
            app_install, app_module, app_v0 = classic_install(
                software, modules, "app/1", "app-v0\n", "all/app/1.lua", name="app", loads=("lib/1",)
            )
            lib_inode = os.lstat(lib_install).st_ino
            app_inode = os.lstat(app_install).st_ino
            packages = [
                {
                    "rel": "lib/1",
                    "payload": "lib-v1\n",
                    "module_rel": "all/lib/1.lua",
                    "name": "lib",
                },
                {
                    "rel": "app/1",
                    "payload": "app-v1\n",
                    "module_rel": "all/app/1.lua",
                    "name": "app",
                    "loads": ("lib/1",),
                },
            ]
            builder = installpath_builder(packages)
            record = _spec(software, modules, state, builder, "record", ["lib/1", "app/1"])
            self.assertEqual(run(record).returncode, 0)
            self.assertLess(
                record.faults.reached.index("publish.file:all/lib/1.lua.rename"),
                record.faults.reached.index("publish.file:all/app/1.lua.rename"),
            )
            steps = list(record.faults.reached)

        lib_v1 = lua(generation_of("ignored", "run1", "lib/1"), name="lib")
        # generation path depends on the temp root, so the exact v1 text is checked
        # after resume by payload, and during the crash by "a complete payload".
        del lib_v1

        for step in steps:
            with self.subTest(step=step):
                with tempfile.TemporaryDirectory() as root:
                    software, modules, state = _tree(root)
                    lib_install, lib_module, _lib_v0 = classic_install(
                        software, modules, "lib/1", "lib-v0\n", "all/lib/1.lua", name="lib"
                    )
                    app_install, app_module, _app_v0 = classic_install(
                        software,
                        modules,
                        "app/1",
                        "app-v0\n",
                        "all/app/1.lua",
                        name="app",
                        loads=("lib/1",),
                    )
                    lib_inode = os.lstat(lib_install).st_ino
                    app_inode = os.lstat(app_install).st_ino
                    builder = installpath_builder(packages)
                    spec = _spec(
                        software,
                        modules,
                        state,
                        builder,
                        "run1",
                        ["lib/1", "app/1"],
                        faults=FaultInjector(step),
                    )
                    with self.assertRaises(InjectedCrash):
                        run(spec)
                    self.assertEqual(os.lstat(lib_install).st_ino, lib_inode)
                    self.assertEqual(os.lstat(app_install).st_ino, app_inode)
                    for module, install, old, new in (
                        (lib_module, lib_install, "lib-v0\n", "lib-v1\n"),
                        (app_module, app_install, "app-v0\n", "app-v1\n"),
                    ):
                        self.assertEqual(lstat_kind(module), "file")
                        text = open(module, encoding="utf-8").read()
                        _syntax, resolved = parse_root(text)
                        self.assertEqual(lstat_kind(resolved), "dir")
                        payload = open(os.path.join(resolved, "PAYLOAD"), encoding="utf-8").read()
                        self.assertIn(payload, {old, new})
                        if payload == old:
                            self.assertEqual(abspath(resolved), abspath(install))
                    resumed = None
                    journal = os.path.join(state, "runs", "run1", "journal.json")
                    if os.path.isfile(journal):
                        resumed = resume(state, "run1", builder=builder, faults=FaultInjector())
                        self.assertEqual(resumed.returncode, 0, resumed.message)
                        for module, new in ((lib_module, "lib-v1\n"), (app_module, "app-v1\n")):
                            _syntax, resolved = parse_root(open(module, encoding="utf-8").read())
                            self.assertEqual(
                                open(os.path.join(resolved, "PAYLOAD"), encoding="utf-8").read(),
                                new,
                            )

    def test_new_module_is_absent_or_complete(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state = _tree(root)
            builder = installpath_builder(
                [{"rel": "demo/1", "payload": "v1\n", "module_rel": "all/demo/1.lua"}]
            )
            record = _spec(software, modules, state, builder, "record", ["demo/1"])
            self.assertEqual(run(record).returncode, 0)
            steps = list(record.faults.reached)
            module = os.path.join(modules, "all/demo/1.lua")
            self.assertFalse(os.path.exists(os.path.join(software, "demo/1")))

        for step in steps:
            with self.subTest(step=step):
                with tempfile.TemporaryDirectory() as root:
                    software, modules, state = _tree(root)
                    module = os.path.join(modules, "all/demo/1.lua")
                    builder = installpath_builder(
                        [{"rel": "demo/1", "payload": "v1\n", "module_rel": "all/demo/1.lua"}]
                    )
                    spec = _spec(
                        software,
                        modules,
                        state,
                        builder,
                        "run1",
                        ["demo/1"],
                        faults=FaultInjector(step),
                    )
                    with self.assertRaises(InjectedCrash):
                        run(spec)
                    self.assertFalse(os.path.exists(os.path.join(software, "demo/1")))
                    if os.path.lexists(module):
                        self.assertEqual(lstat_kind(module), "file")
                        text = open(module, encoding="utf-8").read()
                        _syntax, resolved = parse_root(text)
                        self.assertEqual(
                            open(os.path.join(resolved, "PAYLOAD"), encoding="utf-8").read(),
                            "v1\n",
                        )
                        self.assertEqual(lstat_kind(resolved), "dir")

    def test_rollback_restores_previous_module_and_keeps_generation(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, install, module, v0, v1, inode, builder = self._one(root)
            spec = _spec(software, modules, state, builder, "run1", ["demo/1"])
            self.assertEqual(run(spec).returncode, 0)
            generation = generation_of(state, "run1", "demo/1")
            self.assertTrue(os.path.isdir(generation))
            result = rollback(state, "run1")
            self.assertEqual(result.returncode, 0, result.message)
            self.assertEqual(open(module, encoding="utf-8").read(), v0)
            self.assertEqual(os.lstat(install).st_ino, inode)
            self.assertTrue(os.path.isdir(generation))
            self.assertEqual(open(os.path.join(generation, "PAYLOAD"), encoding="utf-8").read(), "v1\n")
            again = rollback(state, "run1")
            self.assertEqual(again.returncode, 0)
            self.assertEqual(open(module, encoding="utf-8").read(), v0)

    def test_rollback_fault_then_resume(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, install, module, v0, v1, inode, builder = self._one(root)
            self.assertEqual(run(_spec(software, modules, state, builder, "run1", ["demo/1"])).returncode, 0)
            with self.assertRaises(InjectedCrash):
                rollback(state, "run1", faults=FaultInjector("rollback.file:all/demo/1.lua.rename"))
            self._check(module, v0, v1, install, inode)
            result = rollback(state, "run1", faults=FaultInjector())
            self.assertEqual(result.returncode, 0)
            self.assertEqual(open(module, encoding="utf-8").read(), v0)

    def test_symlink_install_dir_is_refused_before_build(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state = _tree(root)
            real = os.path.join(root, "real")
            os.makedirs(real)
            with open(os.path.join(real, "PAYLOAD"), "w", encoding="utf-8") as handle:
                handle.write("kept\n")
            os.makedirs(os.path.join(software, "demo"))
            os.symlink(real, os.path.join(software, "demo", "1"))
            called = []

            def builder(_ctx):
                called.append(True)
                return 0

            spec = _spec(software, modules, state, builder, "run1", ["demo/1"])
            with self.assertRaises(Refused) as caught:
                run(spec)
            self.assertIn("symlink", str(caught.exception))
            self.assertEqual(called, [])
            self.assertEqual(open(os.path.join(real, "PAYLOAD"), encoding="utf-8").read(), "kept\n")

    def test_symlink_module_file_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, install, module, v0, _v1, _inode, builder = self._one(root)
            target = module + ".target"
            os.rename(module, target)
            os.symlink(target, module)
            before = os.readlink(module)
            spec = _spec(software, modules, state, builder, "run1", ["demo/1"])
            result = run(spec)
            self.assertEqual(result.returncode, 2)
            self.assertTrue(os.path.islink(module))
            self.assertEqual(os.readlink(module), before)
            self.assertEqual(open(target, encoding="utf-8").read(), v0)
            self.assertEqual(open(os.path.join(install, "PAYLOAD"), encoding="utf-8").read(), "v0\n")

    def test_preserves_module_file_mode(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, _install, module, _v0, v1, _inode, builder = self._one(root)
            os.chmod(module, 0o640)
            spec = _spec(software, modules, state, builder, "run1", ["demo/1"])
            self.assertEqual(run(spec).returncode, 0)
            self.assertEqual(os.stat(module).st_mode & 0o777, 0o640)
            self.assertEqual(open(module, encoding="utf-8").read(), v1)

    def test_gc_removes_only_a_retired_generation(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, install, module, v0, _v1, inode, builder = self._one(root)
            clock = Clock()
            first = _spec(software, modules, state, builder, "run1", ["demo/1"], clock=clock)
            self.assertEqual(run(first).returncode, 0)
            second_builder = installpath_builder(
                [
                    {
                        "rel": "demo/1",
                        "payload": "v2\n",
                        "module_rel": "all/demo/1.lua",
                    }
                ]
            )
            second = _spec(software, modules, state, second_builder, "run2", ["demo/1"], clock=clock)
            self.assertEqual(run(second).returncode, 0)
            self.assertEqual(
                open(os.path.join(parse_root(open(module, encoding="utf-8").read())[1], "PAYLOAD"), encoding="utf-8").read(),
                "v2\n",
            )
            grace = 144
            # Still inside the grace window measured from the second publish.
            early = collect(state, grace, clock.now + 10, dry_run=True)
            self.assertTrue(all(item["action"] == "keep" for item in early))
            self.assertTrue(os.path.isdir(generation_of(state, "run1", "demo/1")))
            later = collect(state, grace, clock.now + grace * 3600 + 5, dry_run=False)
            removed = [item["path"] for item in later if item["action"] == "remove"]
            self.assertTrue(any(item.endswith(os.path.join("generations", "run1")) for item in removed))
            self.assertFalse(os.path.isdir(os.path.join(state, "generations", "run1")))
            self.assertTrue(os.path.isdir(os.path.join(state, "generations", "run2")))
            self.assertEqual(os.lstat(install).st_ino, inode)
            self.assertEqual(open(os.path.join(install, "PAYLOAD"), encoding="utf-8").read(), "v0\n")
            self.assertTrue(os.path.isfile(module))

    def test_failed_build_does_not_publish(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, install, module, v0, _v1, inode, _builder = self._one(root)

            def fail(_ctx):
                return 7

            spec = _spec(software, modules, state, fail, "run1", ["demo/1"])
            result = run(spec)
            self.assertEqual(result.returncode, 7)
            self.assertEqual(open(module, encoding="utf-8").read(), v0)
            self.assertEqual(os.lstat(install).st_ino, inode)

    def test_resume_clears_staged_module_files_before_rebuilding(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, install, _module, _v0, _v1, _inode, _builder = self._one(root)
            seen = []

            def builder(ctx):
                staged = os.path.join(ctx.generation_root, "modules", "all", "demo", "1.lua")
                seen.append(os.path.isfile(staged))
                os.makedirs(os.path.dirname(staged), exist_ok=True)
                with open(staged, "w", encoding="utf-8") as handle:
                    handle.write(f'local root = "{install}"\n')
                generation = os.path.join(ctx.generation_root, "software", "demo", "1", "easybuild")
                os.makedirs(generation, exist_ok=True)
                with open(os.path.join(generation, "demo-1.eb"), "w", encoding="utf-8") as handle:
                    handle.write("name = 'demo'\n")
                with open(os.path.join(ctx.generation_root, "software", "demo", "1", "PAYLOAD"), "w", encoding="utf-8") as handle:
                    handle.write("v1\n")
                return 0

            spec = _spec(
                software,
                modules,
                state,
                builder,
                "run1",
                ["demo/1"],
                faults=FaultInjector("build.finish"),
            )
            with self.assertRaises(InjectedCrash):
                run(spec)
            with self.assertRaises(InjectedCrash):
                resume(state, "run1", builder=builder, faults=FaultInjector("build.finish"))
            self.assertEqual(seen, [False, False])
            self.assertTrue(os.path.isdir(install))

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, _install, module, v0, _v1, _inode, builder = self._one(root)
            spec = _spec(software, modules, state, builder, "run1", ["demo/1"])
            spec.dry_run = True
            result = run(spec)
            self.assertEqual(result.returncode, 0, result.message)
            self.assertFalse(os.path.isdir(os.path.join(state, "runs")))
            self.assertEqual(open(module, encoding="utf-8").read(), v0)
            self.assertIn("--installpath-software=", result.command)
            self.assertNotIn("--remove-ghost-install-dirs", result.command)


class RejectedTwoRenameTest(unittest.TestCase):
    def test_failed_second_rename_leaves_the_live_path_missing(self):
        """The directory-exchange fallback can drop the live name. This tool does not do it."""
        with tempfile.TemporaryDirectory() as root:
            live = os.path.join(root, "demo")
            new = os.path.join(root, ".demo.new")
            os.makedirs(live)
            os.makedirs(new)
            open(os.path.join(live, "PAYLOAD"), "w", encoding="utf-8").write("old\n")
            open(os.path.join(new, "PAYLOAD"), "w", encoding="utf-8").write("new\n")
            held = new + ".old"
            os.rename(live, held)
            self.assertFalse(os.path.exists(live))
            # The second rename is where a crash or an EXDEV leaves the name missing.
            self.assertTrue(os.path.isdir(held))
            self.assertFalse(os.path.exists(live))


if __name__ == "__main__":
    unittest.main()

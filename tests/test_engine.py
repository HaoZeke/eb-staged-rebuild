"""End-to-end transaction tests, including every recorded fault point."""

import json
import os
import tempfile
import unittest

from eb_staged_rebuild.engine import Spec, resume, rollback, run
from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.faults import FaultInjector, InjectedCrash
from eb_staged_rebuild.gc import collect
from eb_staged_rebuild.journal import journal_path, load_journal
from eb_staged_rebuild.modulefile import parse_root
from eb_staged_rebuild.paths import lstat_kind
from eb_staged_rebuild.swap import FALLBACK_HOLD_SUFFIX
from tests.support import PROBES, classic_install, installpath_builder


class Clock:
    def __init__(self):
        self.now = 1_700_000_000.0

    def __call__(self):
        self.now += 10
        return self.now


def _fixture(root, *, two=False):
    software = os.path.join(root, "software")
    modules = os.path.join(root, "modules")
    state = os.path.join(root, "state")
    os.makedirs(software)
    os.makedirs(modules)
    packages = []
    installed = {}
    rows = (
        ("lib", "lib-v0\n", "lib-v1\n"),
        ("app", "app-v0\n", "app-v1\n"),
    ) if two else (("demo", "v0\n", "v1\n"),)
    for name, old, new in rows:
        rel = f"{name}/1"
        module_rel = f"all/{name}/1.lua"
        install, module, module_text = classic_install(
            software,
            modules,
            rel,
            old,
            module_rel,
            name=name,
            loads=("lib/1",) if name == "app" else (),
        )
        installed[rel] = {
            "install": install,
            "module": module,
            "module_text": module_text,
            "inode": os.lstat(install).st_ino,
            "old": old,
            "new": new,
        }
        packages.append(
            {
                "rel": rel,
                "payload": new,
                "module_rel": module_rel,
                "name": name,
                "loads": ("lib/1",) if name == "app" else (),
            }
        )
    return software, modules, state, installed, installpath_builder(packages)


def _spec(
    software,
    modules,
    state,
    builder,
    *,
    run_id="run1",
    faults=None,
    probes=None,
    clock=None,
):
    rels = ["lib/1", "app/1"] if os.path.isdir(os.path.join(software, "lib")) else ["demo/1"]
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
        probes=probes or PROBES,
        clock=clock,
    )


def _payload(path):
    with open(os.path.join(path, "PAYLOAD"), encoding="utf-8") as handle:
        return handle.read()


class TransactionTests(unittest.TestCase):
    def assert_old_live(self, installed):
        for row in installed.values():
            self.assertEqual(lstat_kind(row["install"]), "dir")
            self.assertEqual(os.lstat(row["install"]).st_ino, row["inode"])
            self.assertEqual(_payload(row["install"]), row["old"])
            with open(row["module"], encoding="utf-8") as handle:
                self.assertEqual(handle.read(), row["module_text"])
            parent = os.path.dirname(row["install"])
            for name in os.listdir(parent):
                if name.startswith(f".{os.path.basename(row['install'])}."):
                    self.assertFalse(
                        os.path.lexists(os.path.join(parent, name) + FALLBACK_HOLD_SUFFIX)
                    )

    def assert_new_live(self, installed):
        for row in installed.values():
            self.assertEqual(lstat_kind(row["install"]), "dir")
            self.assertNotEqual(os.lstat(row["install"]).st_ino, row["inode"])
            self.assertEqual(_payload(row["install"]), row["new"])
            with open(row["module"], encoding="utf-8") as handle:
                module_text = handle.read()
            self.assertEqual(parse_root(module_text)[1], row["install"])

    def test_success_uses_fallback_and_keeps_old_tree_for_rollback(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, installed, builder = _fixture(root)
            result = run(_spec(software, modules, state, builder))
            self.assertEqual(result.returncode, 0, result.message)
            self.assert_new_live(installed)
            journal = load_journal(journal_path(state, "run1"))
            mod = journal["modules_plan"][0]
            self.assertEqual(mod["swap_method"], "rename-fallback")
            self.assertEqual(_payload(mod["retired"]), "v0\n")

    def test_success_uses_rename_exchange_when_probe_succeeds(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, installed, builder = _fixture(root)
            probes = json.loads(json.dumps(PROBES))
            probes["rename_exchange"]["ok"] = True
            result = run(_spec(software, modules, state, builder, probes=probes))
            self.assertEqual(result.returncode, 0, result.message)
            self.assert_new_live(installed)
            journal = load_journal(journal_path(state, "run1"))
            self.assertEqual(
                journal["modules_plan"][0]["swap_method"],
                "renameat2-exchange",
            )

    def test_every_recorded_failure_restores_old_tree_then_resumes(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, _installed, builder = _fixture(root)
            recording = _spec(
                software,
                modules,
                state,
                builder,
                run_id="record",
            )
            self.assertEqual(run(recording).returncode, 0)
            steps = list(recording.faults.reached)
            self.assertIn("swap:all/demo/1.lua.fallback.staged-to-live", steps)
            self.assertIn("journal.swap:all/demo/1.lua.rename", steps)
            self.assertIn("journal.complete.rename", steps)

        for step in steps:
            with self.subTest(step=step):
                with tempfile.TemporaryDirectory() as root:
                    software, modules, state, installed, builder = _fixture(root)
                    spec = _spec(
                        software,
                        modules,
                        state,
                        builder,
                        faults=FaultInjector(step),
                    )
                    with self.assertRaises(InjectedCrash):
                        run(spec)
                    self.assert_old_live(installed)
                    path = journal_path(state, "run1")
                    if os.path.isfile(path):
                        result = resume(
                            state,
                            "run1",
                            builder=builder,
                            faults=FaultInjector(),
                        )
                        self.assertEqual(result.returncode, 0, result.message)
                        self.assert_new_live(installed)

    def test_failure_on_second_module_restores_both_modules(self):
        failures = (
            "swap:all/app/1.lua.fallback.staged-to-live",
            "journal.swap:all/app/1.lua.rename",
            "journal.module:all/app/1.lua.rename",
        )
        for step in failures:
            with self.subTest(step=step):
                with tempfile.TemporaryDirectory() as root:
                    software, modules, state, installed, builder = _fixture(root, two=True)
                    with self.assertRaises(InjectedCrash):
                        run(
                            _spec(
                                software,
                                modules,
                                state,
                                builder,
                                faults=FaultInjector(step),
                            )
                        )
                    self.assert_old_live(installed)

    def test_failed_build_never_touches_live_tree(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, installed, _builder = _fixture(root)

            def fail(_context):
                return 7

            result = run(_spec(software, modules, state, fail))
            self.assertEqual(result.returncode, 7)
            self.assert_old_live(installed)

    def test_dry_run_creates_no_state_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, installed, builder = _fixture(root)
            spec = _spec(software, modules, state, builder)
            spec.dry_run = True
            result = run(spec)
            self.assertEqual(result.returncode, 0)
            self.assertFalse(os.path.isdir(os.path.join(state, "runs")))
            self.assert_old_live(installed)
            self.assertIn("installpath-software", result.command)

    def test_missing_rebuild_target_is_refused_before_build(self):
        with tempfile.TemporaryDirectory() as root:
            software = os.path.join(root, "software")
            modules = os.path.join(root, "modules")
            state = os.path.join(root, "state")
            os.makedirs(software)
            os.makedirs(modules)
            called = []

            def builder(_context):
                called.append(True)
                return 0

            with self.assertRaises(Refused):
                run(_spec(software, modules, state, builder))
            self.assertEqual(called, [])

    def test_symlink_rebuild_target_is_refused_before_build(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, installed, builder = _fixture(root)
            row = installed["demo/1"]
            moved = row["install"] + ".real"
            os.rename(row["install"], moved)
            os.symlink(moved, row["install"])
            with self.assertRaises(Refused):
                run(_spec(software, modules, state, builder))
            self.assertEqual(_payload(moved), row["old"])

    def test_module_path_cannot_escape_software_root(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, _installed, builder = _fixture(root)
            spec = _spec(software, modules, state, builder)
            spec.module_rels = ["../outside/1"]
            with self.assertRaises(Refused) as caught:
                run(spec)
            self.assertIn("escapes", str(caught.exception))

    def test_symlinked_state_root_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, _state, installed, builder = _fixture(root)
            real_state = os.path.join(root, "real-state")
            os.makedirs(real_state)
            state = os.path.join(root, "state-link")
            os.symlink(real_state, state)
            with self.assertRaises(Refused) as caught:
                run(_spec(software, modules, state, builder))
            self.assertIn("state root", str(caught.exception))
            self.assert_old_live(installed)


class RollbackTests(unittest.TestCase):
    def test_rollback_restores_exact_old_directory(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, installed, builder = _fixture(root)
            self.assertEqual(run(_spec(software, modules, state, builder)).returncode, 0)
            result = rollback(state, "run1")
            self.assertEqual(result.returncode, 0, result.message)
            TransactionTests().assert_old_live(installed)
            journal = load_journal(journal_path(state, "run1"))
            self.assertEqual(_payload(journal["modules_plan"][0]["retired"]), "v1\n")
            self.assertEqual(rollback(state, "run1").returncode, 0)

    def test_every_rollback_failure_restores_published_tree(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, _installed, builder = _fixture(root)
            self.assertEqual(run(_spec(software, modules, state, builder)).returncode, 0)
            recorder = FaultInjector()
            self.assertEqual(rollback(state, "run1", faults=recorder).returncode, 0)
            steps = list(recorder.reached)
            self.assertIn(
                "rollback.swap:all/demo/1.lua.fallback.staged-to-live",
                steps,
            )
            self.assertIn("journal.rolled_back.rename", steps)

        for step in steps:
            with self.subTest(step=step):
                with tempfile.TemporaryDirectory() as root:
                    software, modules, state, installed, builder = _fixture(root)
                    self.assertEqual(run(_spec(software, modules, state, builder)).returncode, 0)
                    with self.assertRaises(InjectedCrash):
                        rollback(state, "run1", faults=FaultInjector(step))
                    TransactionTests().assert_new_live(installed)
                    self.assertEqual(rollback(state, "run1").returncode, 0)
                    TransactionTests().assert_old_live(installed)


class GarbageCollectionTests(unittest.TestCase):
    def test_gc_removes_only_the_hidden_retired_tree_after_grace(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, installed, builder = _fixture(root)
            clock = Clock()
            self.assertEqual(
                run(_spec(software, modules, state, builder, clock=clock)).returncode,
                0,
            )
            journal = load_journal(journal_path(state, "run1"))
            retired = journal["modules_plan"][0]["retired"]
            grace_hours = 144
            early = collect(state, grace_hours, clock.now, dry_run=True)
            self.assertEqual(early[0]["action"], "keep")
            later = collect(
                state,
                grace_hours,
                clock.now + grace_hours * 3600 + 1,
                dry_run=False,
            )
            self.assertEqual(later[0]["action"], "remove")
            self.assertFalse(os.path.exists(retired))
            TransactionTests().assert_new_live(installed)


if __name__ == "__main__":
    unittest.main()

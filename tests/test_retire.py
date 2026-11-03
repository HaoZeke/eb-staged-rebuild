"""retire: move a module file aside, roll it back, and let gc see the software directory."""

import os
import tempfile
import unittest

from eb_staged_rebuild.engine import Spec, rollback, run
from eb_staged_rebuild.errors import NeedsIntervention, Refused
from eb_staged_rebuild.faults import FaultInjector, InjectedCrash
from eb_staged_rebuild.gc import collect
from eb_staged_rebuild.journal import list_journals
from eb_staged_rebuild.lockfile import FileLock
from eb_staged_rebuild.paths import lstat_kind
from eb_staged_rebuild.retire import retire
from tests.support import PROBES, classic_install, installpath_builder

GRACE = 144
T0 = 1_700_000_000.0


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        self.now += 1
        return self.now


def _tree(root):
    software = os.path.join(root, "software")
    modules = os.path.join(root, "modules")
    state = os.path.join(root, "state")
    os.makedirs(software)
    os.makedirs(modules)
    install, module, text = classic_install(software, modules, "demo/1", "v0\n", "all/demo/1.lua")
    return software, modules, state, install, module, text


def _journal(state, run_id):
    return [j for j in list_journals(state) if j["run_id"] == run_id][0]


class RetireTests(unittest.TestCase):
    def test_retire_moves_module_and_leaves_software(self):
        with tempfile.TemporaryDirectory() as root:
            _sw, _mods, state, install, module, text = _tree(root)
            inode = os.lstat(install).st_ino
            result = retire(state, module, reason="published on cvmfs", clock=Clock())
            self.assertEqual(result.returncode, 0)
            self.assertFalse(os.path.lexists(module))
            journal = _journal(state, result.run_id)
            self.assertEqual(journal["state"], "retired")
            self.assertEqual(journal["module_path"], module)
            self.assertEqual(journal["software_dir"], install)
            self.assertEqual(journal["reason"], "published on cvmfs")
            self.assertIsNotNone(journal["retired_at"])
            moved = journal["retired_path"]
            self.assertTrue(moved.startswith(os.path.join(state, "retired") + os.sep))
            self.assertTrue(moved.endswith(module.lstrip(os.sep)))
            self.assertEqual(open(moved, encoding="utf-8").read(), text)
            self.assertEqual(os.lstat(install).st_ino, inode)
            self.assertEqual(open(os.path.join(install, "PAYLOAD"), encoding="utf-8").read(), "v0\n")

    def test_dry_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            _sw, _mods, state, _install, module, text = _tree(root)
            result = retire(state, module, dry_run=True)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(open(module, encoding="utf-8").read(), text)
            self.assertFalse(os.path.exists(state))

    def test_dry_run_still_refuses(self):
        with tempfile.TemporaryDirectory() as root:
            _sw, mods, state, _install, _module, _text = _tree(root)
            with self.assertRaises(Refused):
                retire(state, os.path.join(mods, "all"), dry_run=True)

    def test_rollback_puts_module_back(self):
        with tempfile.TemporaryDirectory() as root:
            _sw, _mods, state, _install, module, text = _tree(root)
            result = retire(state, module, clock=Clock())
            back = rollback(state, result.run_id, clock=Clock(T0 + 100))
            self.assertEqual(back.returncode, 0)
            self.assertEqual(open(module, encoding="utf-8").read(), text)
            journal = _journal(state, result.run_id)
            self.assertEqual(journal["state"], "rolled_back")
            self.assertIsNotNone(journal["rolled_back_at"])
            self.assertFalse(os.path.lexists(journal["retired_path"]))
            again = rollback(state, result.run_id)
            self.assertEqual(again.returncode, 0)
            self.assertEqual(open(module, encoding="utf-8").read(), text)

    def test_rollback_refuses_to_overwrite_a_new_module_file(self):
        with tempfile.TemporaryDirectory() as root:
            _sw, _mods, state, _install, module, _text = _tree(root)
            result = retire(state, module, clock=Clock())
            with open(module, "w", encoding="utf-8") as handle:
                handle.write('local root = "/elsewhere"\n')
            with self.assertRaises(NeedsIntervention):
                rollback(state, result.run_id)
            self.assertEqual(open(module, encoding="utf-8").read(), 'local root = "/elsewhere"\n')

    def test_retire_again_after_rollback(self):
        with tempfile.TemporaryDirectory() as root:
            _sw, _mods, state, _install, module, _text = _tree(root)
            clock = Clock()
            first = retire(state, module, clock=clock)
            rollback(state, first.run_id, clock=clock)
            second = retire(state, module, clock=clock)
            self.assertNotEqual(first.run_id, second.run_id)
            self.assertFalse(os.path.lexists(module))

    def test_refuses_already_retired(self):
        with tempfile.TemporaryDirectory() as root:
            _sw, _mods, state, _install, module, _text = _tree(root)
            retire(state, module, clock=Clock())
            with self.assertRaises(Refused) as ctx:
                retire(state, module)
            self.assertIn("already retired", str(ctx.exception))

    def test_refuses_non_regular_files(self):
        with tempfile.TemporaryDirectory() as root:
            _sw, mods, state, _install, module, _text = _tree(root)
            link = os.path.join(mods, "all", "demo", "link.lua")
            os.symlink(module, link)
            for target in (link, os.path.join(mods, "all"), os.path.join(mods, "missing.lua")):
                with self.assertRaises(Refused, msg=target):
                    retire(state, target)
            self.assertTrue(os.path.islink(link))
            self.assertTrue(os.path.isfile(module))

    def test_refuses_module_without_root(self):
        with tempfile.TemporaryDirectory() as root:
            _sw, mods, state, _install, _module, _text = _tree(root)
            odd = os.path.join(mods, "odd.lua")
            with open(odd, "w", encoding="utf-8") as handle:
                handle.write('whatis("x")\n')
            with self.assertRaises(Refused):
                retire(state, odd)
            self.assertTrue(os.path.isfile(odd))

    def test_refuses_when_lock_is_held(self):
        with tempfile.TemporaryDirectory() as root:
            _sw, _mods, state, _install, module, text = _tree(root)
            lock = FileLock(state)
            lock.acquire()
            try:
                with self.assertRaises(Refused) as ctx:
                    retire(state, module)
                self.assertIn("lock", str(ctx.exception))
            finally:
                lock.release()
            self.assertEqual(open(module, encoding="utf-8").read(), text)

    def test_refuses_different_filesystem(self):
        shm = "/dev/shm"
        if not os.path.isdir(shm) or os.stat(shm).st_dev == os.stat(tempfile.gettempdir()).st_dev:
            self.skipTest("no second filesystem available")
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory(dir=shm) as other:
            _sw, _mods, _state, _install, module, text = _tree(root)
            state = os.path.join(other, "state")
            with self.assertRaises(Refused) as ctx:
                retire(state, module)
            self.assertIn("different filesystems", str(ctx.exception))
            self.assertEqual(open(module, encoding="utf-8").read(), text)
            self.assertEqual(os.listdir(os.path.join(state, "retired")), [])

    def test_crash_before_rename_leaves_module_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as root:
            _sw, _mods, state, _install, module, text = _tree(root)
            with self.assertRaises(InjectedCrash):
                retire(state, module, faults=FaultInjector("retire.rename"), clock=Clock())
            self.assertEqual(open(module, encoding="utf-8").read(), text)
            journal = list_journals(state)[0]
            self.assertEqual(journal["state"], "retiring")
            with self.assertRaises(Refused):
                retire(state, module)
            back = rollback(state, journal["run_id"])
            self.assertEqual(back.returncode, 0)
            self.assertEqual(open(module, encoding="utf-8").read(), text)

    def test_crash_after_rename_rolls_back(self):
        with tempfile.TemporaryDirectory() as root:
            _sw, _mods, state, _install, module, text = _tree(root)
            with self.assertRaises(InjectedCrash):
                retire(state, module, faults=FaultInjector("journal.retired.write"), clock=Clock())
            self.assertFalse(os.path.lexists(module))
            journal = list_journals(state)[0]
            self.assertEqual(journal["state"], "retiring")
            rollback(state, journal["run_id"])
            self.assertEqual(open(module, encoding="utf-8").read(), text)


class RetireGcTests(unittest.TestCase):
    def test_software_dir_outside_generations_is_reported_not_deleted(self):
        with tempfile.TemporaryDirectory() as root:
            _sw, _mods, state, install, module, _text = _tree(root)
            result = retire(state, module, clock=Clock())
            retired_at = _journal(state, result.run_id)["retired_at"]
            before = collect(state, GRACE, retired_at + 10)
            item = [a for a in before if a["path"] == install][0]
            self.assertEqual(item["action"], "keep")
            self.assertIn("ready to remove by hand after", item["reason"])
            after = collect(state, GRACE, retired_at + GRACE * 3600 + 1)
            item = [a for a in after if a["path"] == install][0]
            self.assertEqual(item["action"], "manual")
            self.assertIn("ready to remove by hand after", item["reason"])
            self.assertTrue(os.path.isdir(install))
            self.assertTrue(os.path.isfile(os.path.join(install, "PAYLOAD")))

    def test_rolled_back_retire_keeps_directory_referenced(self):
        with tempfile.TemporaryDirectory() as root:
            _sw, _mods, state, install, module, _text = _tree(root)
            clock = Clock()
            result = retire(state, module, clock=clock)
            rollback(state, result.run_id, clock=clock)
            actions = collect(state, GRACE, clock.now + GRACE * 3600 * 2)
            self.assertEqual([a for a in actions if a["action"] in ("remove", "manual")], [])

    def test_directory_still_resolved_by_another_module_is_kept(self):
        with tempfile.TemporaryDirectory() as root:
            _sw, mods, state, install, module, text = _tree(root)
            alias = os.path.join(mods, "all", "demo", "alias.lua")
            with open(alias, "w", encoding="utf-8") as handle:
                handle.write(text)
            result = retire(state, module, clock=Clock())
            retired_at = _journal(state, result.run_id)["retired_at"]
            # The alias is not tracked by a journal; retire it too, then restore it
            # through a rolled-back retire so the tool tracks a live file.
            second = retire(state, alias, clock=Clock(retired_at))
            rollback(state, second.run_id, clock=Clock(retired_at))
            actions = collect(state, GRACE, retired_at + GRACE * 3600 + 100)
            item = [a for a in actions if a["path"] == install][0]
            self.assertEqual(item["action"], "keep")
            self.assertIn("live module", item["reason"])

    def test_generation_published_then_retired_is_removed_after_grace(self):
        with tempfile.TemporaryDirectory() as root:
            software = os.path.join(root, "software")
            modules = os.path.join(root, "modules")
            state = os.path.join(root, "state")
            os.makedirs(software)
            os.makedirs(modules)
            classic_install(software, modules, "demo/1", "v0\n", "all/demo/1.lua")
            clock = Clock()
            builder = installpath_builder([{"rel": "demo/1", "payload": "v1\n", "module_rel": "all/demo/1.lua"}])
            spec = Spec(
                software=software,
                modules=modules,
                state_root=state,
                eb_args=["demo-1.eb"],
                module_rels=["demo/1"],
                builder=builder,
                faults=FaultInjector(),
                run_id="run1",
                probes=PROBES,
                clock=clock,
            )
            self.assertEqual(run(spec).returncode, 0)
            generation = os.path.join(state, "generations", "run1")
            module = os.path.join(modules, "all", "demo", "1.lua")
            retired = retire(state, module, clock=clock)
            retired_at = _journal(state, retired.run_id)["retired_at"]
            early = collect(state, GRACE, retired_at + 10)
            self.assertTrue(all(a["action"] == "keep" for a in early))
            self.assertTrue(os.path.isdir(generation))
            late = collect(state, GRACE, retired_at + GRACE * 3600 + 1)
            self.assertIn(generation, [a["path"] for a in late if a["action"] == "remove"])
            self.assertFalse(os.path.exists(generation))
            self.assertEqual(lstat_kind(module), "missing")


if __name__ == "__main__":
    unittest.main()

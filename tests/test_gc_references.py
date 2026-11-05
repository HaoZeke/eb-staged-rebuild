"""gc refuses an unreadable reference and follows a symlink to a hidden tree."""

import os
import tempfile
import unittest

from eb_staged_rebuild.engine import Spec, run
from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.faults import FaultInjector
from eb_staged_rebuild.gc import collect
from eb_staged_rebuild.journal import journal_path, load_journal
from tests.support import PROBES, classic_install, installpath_builder


class ReferenceTests(unittest.TestCase):
    def published(self, root):
        software = os.path.join(root, "software")
        modules = os.path.join(root, "modules")
        state = os.path.join(root, "state")
        os.makedirs(software)
        os.makedirs(modules)
        install, module, _text = classic_install(
            software, modules, "demo/1", "v0\n", "all/demo/1.lua"
        )
        clock = iter(range(1_700_000_000, 1_700_000_000 + 1000, 10)).__next__
        result = run(Spec(
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
            clock=clock,
        ))
        self.assertEqual(result.returncode, 0, result.message)
        journal = load_journal(journal_path(state, "run1"))
        return state, module, journal["modules_plan"][0]["retired"], install

    def test_unreadable_module_is_not_treated_as_unreferenced(self):
        with tempfile.TemporaryDirectory() as root:
            state, module, retired, _install = self.published(root)
            blocked = os.path.dirname(module)
            os.chmod(blocked, 0)
            try:
                with self.assertRaisesRegex(Refused, "cannot inspect live module"):
                    collect(state, 1, 1_800_000_000)
                self.assertTrue(os.path.isdir(retired))
            finally:
                os.chmod(blocked, 0o755)

    def test_symlink_to_a_hidden_tree_keeps_it(self):
        with tempfile.TemporaryDirectory() as root:
            state, module, retired, _install = self.published(root)
            alias = os.path.join(root, "alias")
            os.symlink(retired, alias)
            original = open(module, encoding="utf-8").read()
            root_line = [line for line in original.splitlines() if line.startswith("local root")][0]
            rewritten = original.replace(root_line, f'local root = "{alias}"')
            with open(module, "w", encoding="utf-8") as handle:
                handle.write(rewritten)
            actions = collect(state, 1, 1_800_000_000, dry_run=True)
            matched = [action for action in actions if action["path"] == retired]
            self.assertEqual(matched[0]["action"], "keep")
            self.assertTrue(os.path.isdir(retired))


if __name__ == "__main__":
    unittest.main()

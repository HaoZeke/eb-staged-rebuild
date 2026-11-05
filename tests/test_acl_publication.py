"""Publication and rollback keep each ACL model's bytes, or refuse before a module changes."""

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import tempfile
import unittest

from eb_staged_rebuild.engine import Spec, rollback, run
from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.faults import FaultInjector
from tests.acl_fakes import OPAQUE, fd_path, models
from tests.support import PROBES, classic_install, installpath_builder

PACKAGES = [
    {"rel": "lib/1", "payload": "lib-new\n", "module_rel": "all/lib/1.lua", "name": "lib"},
    {"rel": "app/1", "payload": "app-new\n", "module_rel": "all/app/1.lua", "name": "app",
     "loads": ("lib/1",)},
]

class Hooked(FaultInjector):
    def __init__(self, actions):
        super().__init__()
        self.actions = actions

    def reach(self, step):
        super().reach(step)
        action = self.actions.get(step)
        if action is not None:
            action()

def acl_for(package):
    return OPAQUE + package["rel"].encode()

class AclPublicationTests(unittest.TestCase):
    def fixture(self, directory, model, existing=True):
        root = Path(directory)
        software, modules, state = (root / name for name in ("software", "modules", "state"))
        software.mkdir()
        modules.mkdir()
        lives = {}
        for package in PACKAGES:
            live = modules / package["module_rel"]
            lives[package["module_rel"]] = live
            if existing:
                classic_install(str(software), str(modules), package["rel"], "old\n",
                                package["module_rel"], name=package["name"],
                                loads=package.get("loads", ()))
                model.set_path(live, model.native, acl_for(package))
        build = installpath_builder(PACKAGES)

        def builder(ctx):
            code = build(ctx)
            if not existing:
                for package in PACKAGES:
                    staged = Path(ctx.generation_root) / "modules" / package["module_rel"]
                    model.set_path(staged, model.native, acl_for(package))
            return code

        spec = Spec(software=str(software), modules=str(modules), state_root=str(state),
                    eb_args=["app.eb"], module_rels=[p["rel"] for p in PACKAGES], run_id="acl",
                    probes=PROBES, builder=builder, clock=lambda: 100)
        return spec, lives

    def snapshot(self, model, lives):
        return {rel: (live.read_bytes(), model.get_path(live)) for rel, live in lives.items()
                if live.exists()}

    def assert_no_temporaries(self, spec):
        leftovers = [str(path) for path in Path(spec.modules).rglob(".*.new")]
        self.assertEqual(leftovers, [])

    @contextmanager
    def scenario(self, model, existing=True):
        with self.subTest(model=model.label), tempfile.TemporaryDirectory() as directory:
            with model.installed():
                yield self.fixture(directory, model, existing)

    def test_publication_and_rollback_keep_native_acl_bytes(self):
        for model in models():
            with self.scenario(model) as (spec, lives):
                before = self.snapshot(model, lives)
                result = run(spec)
                self.assertEqual(result.returncode, 0, result.message)
                for package in PACKAGES:
                    live = lives[package["module_rel"]]
                    self.assertNotEqual(live.read_bytes(), before[package["module_rel"]][0])
                    self.assertEqual(model.get_path(live), before[package["module_rel"]][1])
                    self.assertEqual(model.get_path(live)[model.native], acl_for(package))
                restored = rollback(spec.state_root, "acl", clock=lambda: 101)
                self.assertEqual(restored.returncode, 0, restored.message)
                self.assertEqual(self.snapshot(model, lives), before)

    def test_new_modules_take_the_staged_native_acl(self):
        for model in models():
            with self.scenario(model, existing=False) as (spec, lives):
                result = run(spec)
                self.assertEqual(result.returncode, 0, result.message)
                for package in PACKAGES:
                    self.assertEqual(model.get_path(lives[package["module_rel"]])[model.native],
                                     acl_for(package))

    def test_uninspectable_prepared_acl_refuses_before_any_publication(self):
        for model in models():
            with self.scenario(model) as (spec, lives):
                before = self.snapshot(model, lives)
                prepared = Path(spec.state_root) / "runs/acl/prepared/all/app/1.lua"

                def deny(fd, name=None):
                    if fd_path(fd) == str(prepared):
                        raise OSError(errno.EACCES, os.strerror(errno.EACCES))
                    return None

                model.hooks["listxattr"] = deny
                with self.assertRaisesRegex(Refused, "cannot validate publication for all/app/1.lua: .*denied"):
                    run(spec)
                model.hooks.clear()
                self.assertEqual(self.snapshot(model, lives), before)
                self.assert_no_temporaries(spec)

    def test_denied_acl_write_mid_publication_is_refused_and_rolls_back(self):
        for model in models():
            with self.scenario(model) as (spec, lives):
                before = self.snapshot(model, lives)
                live_writes = []

                def deny_second(fd, name):
                    target = fd_path(fd)
                    if target.startswith(spec.modules + os.sep) and target.endswith(".new"):
                        live_writes.append(target)
                        if len(live_writes) == 2:
                            raise OSError(errno.EACCES, os.strerror(errno.EACCES))

                model.hooks["setxattr"] = deny_second
                with self.assertRaisesRegex(Refused, "cannot preserve access metadata"):
                    run(spec)
                model.hooks.clear()
                after = self.snapshot(model, lives)
                changed = [rel for rel in lives if after[rel][0] != before[rel][0]]
                self.assertEqual(len(changed), 1)
                for rel in lives:
                    self.assertEqual(after[rel][1], before[rel][1])
                self.assert_no_temporaries(spec)
                restored = rollback(spec.state_root, "acl", clock=lambda: 101)
                self.assertEqual(restored.returncode, 0, restored.message)
                self.assertEqual(self.snapshot(model, lives), before)

    def test_live_acl_change_during_backup_refuses_that_publication(self):
        for model in models():
            with self.scenario(model) as (spec, lives):
                before = self.snapshot(model, lives)
                target = lives["all/lib/1.lua"]
                spec.faults = Hooked({"publish.backup:all/lib/1.lua.fsync":
                                      lambda: model.set_path(target, model.native, b"site change")})
                with self.assertRaisesRegex(Refused, "changed before"):
                    run(spec)
                self.assertNotIn("publish.file:all/lib/1.lua.write", spec.faults.reached)
                self.assertEqual(target.read_bytes(), before["all/lib/1.lua"][0])
                self.assertEqual(model.get_path(target)[model.native], b"site change")
                self.assertFalse((Path(spec.state_root) / "runs/acl/backups/all/lib/1.lua").exists())
                self.assert_no_temporaries(spec)

    def test_unsupported_destination_model_refuses_publication(self):
        for model in models():
            with self.scenario(model) as (spec, lives):
                before = self.snapshot(model, lives)

                def unsupported(fd, name):
                    target = fd_path(fd)
                    if target.startswith(spec.modules + os.sep) and target.endswith(".new"):
                        raise OSError(errno.EOPNOTSUPP, os.strerror(errno.EOPNOTSUPP))

                model.hooks["setxattr"] = unsupported
                with self.assertRaises(Refused):
                    run(spec)
                model.hooks.clear()
                self.assertEqual(self.snapshot(model, lives), before)
                self.assert_no_temporaries(spec)

if __name__ == "__main__":
    unittest.main()

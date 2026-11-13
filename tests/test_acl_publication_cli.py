"""test_acl_publication against the Zig test artifact.

The Python tests patch the descriptor xattr calls in process. The binary
cannot be patched, so these run the test artifact built with -Dtest-ops
(never a release), which emulates the same ACL models (zig/aclmodel.zig)
and keeps their attributes in a store file. The assertions are the Python
tests' assertions. Skipped unless TOOL_BIN_TEST_OPS names that artifact.
"""

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import acl_store  # noqa: E402
from tests.support import classic_install  # noqa: E402

TOOL = os.environ.get("TOOL_BIN_TEST_OPS")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPAQUE = b"\x00\x01native-acl\xff\xfe\x00" + bytes(range(256))
PACKAGES = [
    {"rel": "lib/1", "payload": "lib-new\n", "module_rel": "all/lib/1.lua", "name": "lib"},
    {"rel": "app/1", "payload": "app-new\n", "module_rel": "all/app/1.lua", "name": "app", "loads": ["lib/1"]},
]

# An EasyBuild stand-in that writes both packages as installpath_builder does,
# with a marker so the module text differs from the live file, and, when asked,
# the staged module's native ACL.
FAKE_EB = r'''
import json, os, sys
sys.path.insert(0, {repo!r})
from tests import acl_store
from tests.support import lua
packages = json.loads({packages!r})
args = sys.argv[1:]
if "--dry-run" in args:
    sys.exit(0)
opt = lambda name: next(a.split("=", 1)[1] for a in args if a.startswith(name + "="))
software, modules = opt("--installpath-software"), opt("--installpath-modules")
store, model = os.environ["EB_STAGED_REBUILD_ACL_STORE"], os.environ["EB_STAGED_REBUILD_ACL_MODEL"]
def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(text)
    acl_store.clear(store, path)
for p in packages:
    generation = os.path.join(software, p["rel"])
    write(os.path.join(generation, "PAYLOAD"), p["payload"])
    write(os.path.join(generation, "easybuild", p["name"] + "-1.eb"), "name = '%s'\n" % p["name"])
    module = os.path.join(modules, p["module_rel"])
    write(module, lua(os.path.realpath(generation), name=p["name"], loads=p.get("loads", ())) + "-- rebuilt\n")
    if os.environ.get("FAKE_EB_STAGED_ACL") == "1":
        acl_store.set_native(store, model, module, bytes.fromhex(os.environ["OPAQUE_HEX"]) + p["rel"].encode())
'''


def acl_for(package):
    return OPAQUE + package["rel"].encode()


@unittest.skipUnless(TOOL, "TOOL_BIN_TEST_OPS names no -Dtest-ops artifact")
class AclPublicationCliTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="acl-publication-")
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)

    def fixture(self, model):
        root = os.path.join(self.base, model)
        software, modules, state = (os.path.join(root, n) for n in ("software", "modules", "state"))
        os.makedirs(software)
        os.makedirs(modules)
        store = os.path.join(root, "acl.store")
        lives = {}
        for package in PACKAGES:
            live = Path(modules) / package["module_rel"]
            lives[package["module_rel"]] = live
            classic_install(software, modules, package["rel"], "old\n", package["module_rel"],
                            name=package["name"], loads=package.get("loads", ()))
            acl_store.set_native(store, model, live, acl_for(package))
        eb = os.path.join(root, "eb")
        import json
        with open(eb, "w") as handle:
            handle.write(f"#!{sys.executable}\n" + FAKE_EB.format(repo=REPO, packages=json.dumps(PACKAGES)))
        os.chmod(eb, 0o755)
        env = dict(os.environ, EB_STAGED_REBUILD_EB=eb, EB_STAGED_REBUILD_ACL_MODEL=model,
                   EB_STAGED_REBUILD_ACL_STORE=store, OPAQUE_HEX=OPAQUE.hex())
        env.pop("EB_STAGED_REBUILD_ACL_FAULT", None)
        return {"root": root, "software": software, "modules": modules, "state": state, "store": store,
                "lives": lives, "env": env, "model": model}

    def run_tool(self, fx, args, **env):
        return subprocess.run([TOOL, *args], cwd=fx["root"], env=dict(fx["env"], **env),
                              capture_output=True, text=True, timeout=300)

    def run_args(self, fx):
        return ["run", "--software", fx["software"], "--modules", fx["modules"], "--state-root", fx["state"],
                "--backend", "installpath", "--module", "lib/1", "--module", "app/1", "--run-id", "acl",
                "--", "app.eb"]

    def snapshot(self, fx):
        return {rel: (live.read_bytes(), acl_store.get(fx["store"], live)) for rel, live in fx["lives"].items()
                if live.exists()}

    def assert_no_temporaries(self, fx):
        self.assertEqual([str(p) for p in Path(fx["modules"]).rglob(".*.new")], [])

    def native(self, fx):
        return acl_store.MODELS[fx["model"]][0]

    def test_publication_and_rollback_keep_native_acl_bytes(self):
        for model in acl_store.MODELS:
            with self.subTest(model=model):
                fx = self.fixture(model)
                before = self.snapshot(fx)
                done = self.run_tool(fx, self.run_args(fx))
                self.assertEqual(done.returncode, 0, done.stderr)
                for package in PACKAGES:
                    live = fx["lives"][package["module_rel"]]
                    self.assertNotEqual(live.read_bytes(), before[package["module_rel"]][0])
                    self.assertEqual(acl_store.get(fx["store"], live), before[package["module_rel"]][1])
                    self.assertEqual(acl_store.get(fx["store"], live)[self.native(fx)], acl_for(package))
                done = self.run_tool(fx, ["rollback", "--state-root", fx["state"], "acl"])
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(self.snapshot(fx), before)

    def test_missing_module_file_takes_the_staged_native_acl(self):
        for model in acl_store.MODELS:
            with self.subTest(model=model):
                fx = self.fixture(model)
                for live in fx["lives"].values():
                    acl_store.clear(fx["store"], live)
                    live.unlink()
                done = self.run_tool(fx, self.run_args(fx), FAKE_EB_STAGED_ACL="1")
                self.assertEqual(done.returncode, 0, done.stderr)
                for package in PACKAGES:
                    self.assertEqual(acl_store.get(fx["store"], fx["lives"][package["module_rel"]])[self.native(fx)],
                                     acl_for(package))

    def test_uninspectable_prepared_acl_refuses_before_any_publication(self):
        for model in acl_store.MODELS:
            with self.subTest(model=model):
                fx = self.fixture(model)
                before = self.snapshot(fx)
                prepared = os.path.join(fx["state"], "runs/acl/prepared/all/app/1.lua")
                done = self.run_tool(fx, self.run_args(fx),
                                     EB_STAGED_REBUILD_ACL_FAULT=f"op=listxattr,path={prepared},errno=EACCES")
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertRegex(done.stderr, "cannot validate publication for all/app/1.lua: .*denied")
                self.assertEqual(self.snapshot(fx), before)
                self.assert_no_temporaries(fx)

    def test_denied_acl_write_mid_publication_is_refused_and_rolls_back(self):
        for model in acl_store.MODELS:
            with self.subTest(model=model):
                fx = self.fixture(model)
                before = self.snapshot(fx)
                done = self.run_tool(fx, self.run_args(fx), EB_STAGED_REBUILD_ACL_FAULT=(
                    f"op=setxattr,under={fx['modules']},suffix=.new,nth=2,errno=EACCES"))
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertIn("cannot preserve access metadata", done.stderr)
                # A caught failure restores every directory swap and module
                # write; the run is unpublished, so a later rollback is refused.
                self.assertEqual(self.snapshot(fx), before)
                self.assert_no_temporaries(fx)
                done = self.run_tool(fx, ["rollback", "--state-root", fx["state"], "acl"])
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertIn("only a complete run can roll back", done.stderr)
                self.assertEqual(self.snapshot(fx), before)

    def test_live_acl_change_during_backup_refuses_that_publication(self):
        for model in acl_store.MODELS:
            with self.subTest(model=model):
                fx = self.fixture(model)
                before = self.snapshot(fx)
                target = fx["lives"]["all/lib/1.lua"]
                ready = os.path.join(fx["root"], "paused")
                child = subprocess.Popen(
                    [TOOL, *self.run_args(fx)], cwd=fx["root"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, env=dict(fx["env"], EB_STAGED_REBUILD_PAUSE_AT="publish.backup:all/lib/1.lua.fsync",
                                        EB_STAGED_REBUILD_PAUSE_READY=ready, EB_STAGED_REBUILD_PAUSE_SECONDS="3"))
                deadline = time.monotonic() + 120
                while not os.path.exists(ready) and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(os.path.exists(ready), "the run did not reach the backup fsync")
                acl_store.set_native(fx["store"], model, target, b"site change")
                _out, err = child.communicate(timeout=120)
                self.assertEqual(child.returncode, 2, err)
                self.assertIn("changed before", err)
                self.assertEqual(target.read_bytes(), before["all/lib/1.lua"][0])
                self.assertEqual(acl_store.get(fx["store"], target)[self.native(fx)], b"site change")
                self.assertFalse(Path(fx["state"], "runs/acl/backups/all/lib/1.lua").exists())
                self.assert_no_temporaries(fx)

    def test_unsupported_destination_model_refuses_publication(self):
        for model in acl_store.MODELS:
            with self.subTest(model=model):
                fx = self.fixture(model)
                before = self.snapshot(fx)
                done = self.run_tool(fx, self.run_args(fx), EB_STAGED_REBUILD_ACL_FAULT=(
                    f"op=setxattr,under={fx['modules']},suffix=.new,errno=EOPNOTSUPP"))
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertEqual(self.snapshot(fx), before)
                self.assert_no_temporaries(fx)


if __name__ == "__main__":
    unittest.main()

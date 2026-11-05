import hashlib
import os
from pathlib import Path
import stat
import tempfile
import unittest
from contextlib import contextmanager
from types import SimpleNamespace

from eb_staged_rebuild.engine import Spec, resume, rollback, run
from eb_staged_rebuild.faults import FaultInjector, InjectedCrash
from eb_staged_rebuild.modulefile import parse_root
from tests.support import PROBES, classic_install, generation_of, installpath_builder


def snapshot(root):
    paths = [root] + sorted(root.rglob("*"))
    return {
        str(path.relative_to(root)): (
            stat.S_IMODE(path.stat().st_mode),
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
        )
        for path in paths
    }


@contextmanager
def sealed_fixture():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        software, modules, state = (root / name for name in ("software", "modules", "state"))
        software.mkdir()
        modules.mkdir()
        original, module, original_text = classic_install(
            str(software), str(modules), "demo/1", "v0\n", "all/demo/1.lua"
        )
        original_snapshot = snapshot(Path(original))
        original_inode = os.stat(original).st_ino
        target = Path(generation_of(str(state), "sealed", "demo/1"))
        sealed_snapshot = []
        build = installpath_builder(
            [{"rel": "demo/1", "payload": "v1\n", "module_rel": "all/demo/1.lua"}]
        )

        def builder(ctx):
            result = build(ctx)
            paths = [target] + list(target.rglob("*"))
            for path in paths:
                path.chmod(0o555 if path.is_dir() else 0o444)
            sealed_snapshot.append(snapshot(target))
            return result

        fixture = SimpleNamespace(
            software=software, modules=modules, state=state, original=Path(original),
            module=Path(module), original_text=original_text,
            original_snapshot=original_snapshot, original_inode=original_inode,
            target=target, sealed_snapshot=sealed_snapshot, builder=builder,
        )
        try:
            yield fixture
        finally:
            if target.exists():
                for path in [target] + list(target.rglob("*")):
                    path.chmod(0o755 if path.is_dir() else 0o644)


class SealedInstallTests(unittest.TestCase):
    def spec(self, fixture, faults=None):
        return Spec(
            software=str(fixture.software), modules=str(fixture.modules),
            state_root=str(fixture.state), eb_args=["demo-1.eb"],
            backend="installpath", module_rels=["demo/1"],
            builder=fixture.builder, run_id="sealed", probes=PROBES,
            faults=faults or FaultInjector(),
        )

    def assert_published_without_package_changes(self, fixture):
        self.assertEqual(parse_root(fixture.module.read_text())[1], str(fixture.target))
        self.assertEqual((fixture.target / "PAYLOAD").read_text(), "v1\n")
        self.assertEqual(snapshot(fixture.target), fixture.sealed_snapshot[0])
        self.assertEqual(snapshot(fixture.original), fixture.original_snapshot)
        self.assertEqual(fixture.original.stat().st_ino, fixture.original_inode)

    def test_publish_preserves_sealed_files_and_permissions(self):
        with sealed_fixture() as fixture:
            result = run(self.spec(fixture))
            self.assertEqual(result.returncode, 0, result.message)
            self.assert_published_without_package_changes(fixture)
            restored = rollback(str(fixture.state), "sealed")
            self.assertEqual(restored.returncode, 0, restored.message)
            self.assertEqual(fixture.module.read_text(), fixture.original_text)
            self.assertEqual(snapshot(fixture.target), fixture.sealed_snapshot[0])

    def test_resume_publishes_sealed_generation_without_rebuilding(self):
        with sealed_fixture() as fixture:
            faults = FaultInjector("publish.file:all/demo/1.lua.rename")
            with self.assertRaises(InjectedCrash):
                run(self.spec(fixture, faults))
            self.assertEqual(fixture.module.read_text(), fixture.original_text)
            restored = resume(
                str(fixture.state), "sealed", builder=fixture.builder,
                faults=FaultInjector(),
            )
            self.assertEqual(restored.returncode, 0, restored.message)
            self.assertEqual(len(fixture.sealed_snapshot), 1)
            self.assert_published_without_package_changes(fixture)


if __name__ == "__main__":
    unittest.main()

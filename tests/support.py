"""Shared fixtures for eb-staged-rebuild tests."""

import os
import stat

from eb_staged_rebuild.atomicio import atomic_write
from eb_staged_rebuild.faults import FaultInjector
from eb_staged_rebuild.paths import abspath

PROBES = {
    "bwrap": {
        "installed": True,
        "usable": True,
        "reason": None,
        "version": "test",
        "path": "bwrap",
    },
    "user_namespaces": {"max_user_namespaces": None, "readable": False},
    "rename_exchange": {"ok": False, "errno": 22, "error": "test", "fstype": "test"},
    "easybuild": {
        "found": True,
        "framework_version": [5, 4, 2],
        "stage_path_keys": True,
        "keys_source": "test",
        "version_text": "test",
        "reason": None,
    },
    "filesystem": {"fstype": "test"},
    "flock": {"cross_node": "unmeasured", "method": "flock"},
}


def lua(root, name="demo", version="1", loads=()):
    lines = [
        f'local root = "{root}"',
        f'conflict("{name}")',
    ]
    for dep in loads:
        lines.append(f'load("{dep}")')
    lines.append(f'setenv("EBROOT{name.upper()}", root)')
    lines.append(f'setenv("EBVERSION{name.upper()}", "{version}")')
    lines.append("")
    return "\n".join(lines)


def classic_install(software, modules, rel, payload, module_rel, name="demo", version="1", loads=()):
    install = os.path.join(software, rel)
    os.makedirs(os.path.join(install, "easybuild"), exist_ok=True)
    silent = FaultInjector()
    atomic_write(os.path.join(install, "PAYLOAD"), payload, silent, "fixture.payload")
    atomic_write(
        os.path.join(install, "easybuild", f"{name}-{version}.eb"),
        f"name = '{name}'\n",
        silent,
        "fixture.eb",
    )
    module = os.path.join(modules, module_rel)
    text = lua(abspath(install), name=name, version=version, loads=loads)
    atomic_write(module, text, silent, "fixture.module")
    return abspath(install), abspath(module), text


def generation_of(state_root, run_id, rel):
    return abspath(os.path.join(state_root, "generations", run_id, "software", rel))


def installpath_builder(packages):
    """In-process EasyBuild stand-in.

    ``packages`` is a list of dicts: rel, payload, module_rel, name, version, loads.
    Fault points ``build.payload:<rel>`` and ``build.module:<rel>`` are raised
    before those files are written.
    """

    def build(ctx):
        silent = FaultInjector()
        for package in packages:
            rel = package["rel"]
            ctx.faults.reach(f"build.payload:{rel}")
            generation = os.path.join(ctx.generation_root, "software", rel)
            os.makedirs(os.path.join(generation, "easybuild"), exist_ok=True)
            atomic_write(
                os.path.join(generation, "PAYLOAD"),
                package["payload"],
                silent,
                "silent.payload",
            )
            name = package.get("name", "demo")
            version = package.get("version", "1")
            atomic_write(
                os.path.join(generation, "easybuild", f"{name}-{version}.eb"),
                f"name = '{name}'\n",
                silent,
                "silent.eb",
            )
            ctx.faults.reach(f"build.module:{rel}")
            module = os.path.join(ctx.generation_root, "modules", package["module_rel"])
            text = lua(
                abspath(generation),
                name=name,
                version=version,
                loads=package.get("loads") or (),
            )
            atomic_write(module, text, silent, "silent.module")
            link_rel = package.get("link_rel")
            if link_rel:
                link = os.path.join(ctx.generation_root, "modules", link_rel)
                os.makedirs(os.path.dirname(link), exist_ok=True)
                if not os.path.lexists(link):
                    os.symlink(abspath(module), link)
        return 0

    return build


def guard_calls(live_dirs):
    """Raise if a rename or delete names a path inside a live install directory."""
    banned = [abspath(path) for path in live_dirs]

    def illegal(path):
        path = abspath(path)
        return any(path == root or path.startswith(root + os.sep) for root in banned)

    def wrap(original):
        def wrapped(*args, **kwargs):
            for arg in args:
                if isinstance(arg, (str, os.PathLike)) and illegal(arg):
                    raise AssertionError(f"{original.__name__} touched live install {arg}")
            return original(*args, **kwargs)

        return wrapped

    return {
        "rename": wrap(os.rename),
        "replace": wrap(os.replace),
        "remove": wrap(os.remove),
        "unlink": wrap(os.unlink),
        "rmdir": wrap(os.rmdir),
    }


def patch_os(guards):
    originals = {name: getattr(os, name) for name in guards}
    for name, wrapped in guards.items():
        setattr(os, name, wrapped)
    return originals


def restore_os(originals):
    for name, original in originals.items():
        setattr(os, name, original)


def mode_of(path):
    return stat.S_IMODE(os.lstat(path).st_mode)

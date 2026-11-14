#!/usr/bin/env python3
"""Stand-in for the eb executable used by unit tests.

It writes a generation the way the installpath and bwrap backends expect, and
it records the argv it was given. It does not build software.
"""

import json
import os
import shutil
import sys


def _option(args, name):
    prefix = name + "="
    for arg in args:
        if arg.startswith(prefix):
            return arg[len(prefix):]
    return None


def _rel():
    return os.environ.get("FAKE_EB_REL", "demo/1")


def _payload():
    return os.environ.get("FAKE_EB_PAYLOAD", "from-eb\n")


def _record(args):
    path = os.environ.get("FAKE_EB_ARGV")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(args) + "\n")


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _lua(root):
    return (
        f'local root = "{root}"\n'
        'conflict("demo")\n'
        'setenv("EBROOTDEMO", root)\n'
        'setenv("EBVERSIONDEMO", "1")\n'
    )


def _unseal(top):
    for directory, dirs, _files in os.walk(top):
        for name in dirs:
            path = os.path.join(directory, name)
            if not os.path.islink(path):
                os.chmod(path, 0o755)
    os.chmod(top, 0o755)


def _write_tree(software, modules, root_in_module, rel=None):
    rel = rel or _rel()
    generation = os.path.join(software, rel)
    # eb --rebuild replaces an installation an earlier attempt left, sealed
    # or not.
    if os.path.isdir(generation) and not os.path.islink(generation):
        _unseal(generation)
        shutil.rmtree(generation)
    _write(os.path.join(generation, "PAYLOAD"), _payload())
    _write(os.path.join(generation, "easybuild", "demo-1.eb"), "name = 'demo'\n")
    module = os.path.join(modules, "all", rel + ".lua")
    _write(module, _lua(root_in_module))
    # FAKE_EB_ACL: hex bytes of a POSIX access ACL set on the written module.
    acl = os.environ.get("FAKE_EB_ACL")
    if acl:
        os.setxattr(module, "system.posix_acl_access", bytes.fromhex(acl))
    link = os.path.join(modules, "tools", rel + ".lua")
    os.makedirs(os.path.dirname(link), exist_ok=True)
    if not os.path.lexists(link):
        os.symlink(module, link)


def main(argv):
    _record(argv)
    # FAKE_EB_VERSION: answer eb --version as that EasyBuild release does.
    version = os.environ.get("FAKE_EB_VERSION")
    if version and "--version" in argv:
        print(f"This is EasyBuild {version} (framework: {version}, easyblocks: {version}) on host fake.")
        return 0
    if "--dry-run" in argv:
        print("Dry run: printing build status of easyconfigs")
        for rel in (os.environ.get("FAKE_EB_RELS") or _rel()).split(","):
            print(f" * [R] {rel.replace('/', '-')}.eb (module: {rel})")
        return 0
    if os.environ.get("FAKE_EB_FAIL") == "1":
        print("fake eb forced failure", file=sys.stderr)
        return 3
    if "--bwrap" in argv:
        stage = _option(argv, "--bwrap-installpath")
        if not stage:
            print("missing --bwrap-installpath", file=sys.stderr)
            return 2
        live_software = os.environ["EASYBUILD_INSTALLPATH_SOFTWARE"]
        rel = _rel()
        software = os.path.join(stage, "software")
        modules = os.path.join(stage, "modules")
        _write_tree(software, modules, os.path.join(live_software, rel))
        keys = {
            "bwrap_cmd": [
                "bwrap",
                "--dev-bind",
                "/",
                "/",
                "--bind",
                os.path.join(software, rel),
                os.path.join(live_software, rel),
            ],
            "bwrap_installpath": stage,
            "installpath_software": live_software,
            "installpath_modules": os.environ.get("EASYBUILD_INSTALLPATH_MODULES", ""),
            "modules_to_install": [rel],
        }
        if os.environ.get("FAKE_EB_OVERLAY") == "1":
            keys["bwrap_cmd"] = ["bwrap", "--overlay", "upper", "work", "dest"]
        if os.environ.get("FAKE_EB_KEYS") != "legacy":
            keys["bwrap_installpath_software"] = software
            keys["bwrap_installpath_modules"] = modules
        _write(os.path.join(stage, "bwrap_info.json"), json.dumps(keys, indent=2, sort_keys=True) + "\n")
        return 0
    software = _option(argv, "--installpath-software")
    modules = _option(argv, "--installpath-modules")
    if not software or not modules:
        print("missing installpath", file=sys.stderr)
        return 2
    # FAKE_EB_RELS: a comma-separated list of modules to write instead of one.
    rels = (os.environ.get("FAKE_EB_RELS") or _rel()).split(",")
    for rel in rels:
        _write_tree(software, modules, os.path.join(software, rel), rel)
    # FAKE_EB_LEDGER_EDIT: a ledger configuration this build changes, to
    # show that the policy was frozen before the build started.
    edit = os.environ.get("FAKE_EB_LEDGER_EDIT")
    if edit:
        with open(edit, encoding="utf-8") as handle:
            policy = json.load(handle)
        policy["actor"] = "different-actor"
        with open(edit, "w", encoding="utf-8") as handle:
            json.dump(policy, handle)
    # FAKE_EB_SEAL=1: leave each installation and the module tree read-only,
    # as an easyconfig that seals its install does.
    # FAKE_EB_SEAL=modules: only the module tree.
    seal = os.environ.get("FAKE_EB_SEAL")
    if seal in ("1", "modules"):
        for top in ([os.path.join(software, rel) for rel in rels] if seal == "1" else []) + [modules]:
            _seal(top)
    return 0


def _seal(top):
    for directory, dirs, files in os.walk(top):
        for name in files:
            path = os.path.join(directory, name)
            if not os.path.islink(path):
                os.chmod(path, 0o444)
        for name in dirs:
            path = os.path.join(directory, name)
            if not os.path.islink(path):
                os.chmod(path, 0o555)
    os.chmod(top, 0o555)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

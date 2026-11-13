#!/usr/bin/env python3
"""Publication access metadata through `run` and `rollback`, from the Python
tool and from TOOL_BIN on the same fixtures, with the kernel's POSIX ACLs.

Each case builds one fixture, copies it twice and runs the same commands on
each copy. Exit codes, stdout and stderr (paths and times normalized) and the
module file's bytes, mode and POSIX access ACL must match.

    TOOL_BIN=zig-out/bin/eb-staged-rebuild python3 tests/differential_metadata.py

The fixtures live below the repository (`.differential-metadata/`), on a file
system that keeps POSIX ACLs; where it does not, the script says so and exits 0.
"""

import errno
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from tests.support import classic_install  # noqa: E402

TOOL = os.environ["TOOL_BIN"]
POSIX = "system.posix_acl_access"


def access_acl():
    entries = [(0x01, 6, 0xffffffff), (0x02, 4, 65534), (0x04, 4, 0xffffffff), (0x10, 4, 0xffffffff),
               (0x20, 0, 0xffffffff)]
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)


def acl_of(path):
    try:
        return os.getxattr(path, POSIX)
    except OSError as error:
        if error.errno == errno.ENODATA:
            return None
        raise


def norm(text, root):
    text = text.replace(root, "<ROOT>")
    text = re.sub(r"[0-9]{8}T[0-9]{6}Z(-[0-9]+)?", "STAMP", text)
    text = re.sub(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", "ISO", text)
    text = re.sub(r"[0-9]{10}\.[0-9]+", "TIME", text)
    text = re.sub(r"[^ ]*python[0-9.]* -m eb_staged_rebuild\.userns_sandbox", "SANDBOX", text)
    return text.replace(f"{TOOL} __userns-sandbox", "SANDBOX")


def fixture(root, existing):
    software, modules, state = (os.path.join(root, n) for n in ("software", "modules", "state"))
    os.makedirs(software)
    os.makedirs(modules)
    module = os.path.join(modules, "all", "demo", "1.lua")
    classic_install(software, modules, "demo/1", "old", "all/demo/1.lua")
    if existing:
        os.chmod(module, 0o600)
    else:
        os.unlink(module)
    eb = os.path.join(root, "eb")
    with open(eb, "w") as handle:
        handle.write(f'#!/bin/sh\nexec "{sys.executable}" "{REPO}/tests/fake_eb.py" "$@"\n')
    os.chmod(eb, 0o755)
    return software, modules, state, module


def policy(root, body):
    path = os.path.join(root, "policy")
    with open(path, "w") as handle:
        handle.write(f"#!{sys.executable}\nimport json, os, sys\nfrom pathlib import Path\n"
                     "context = json.loads(Path(sys.argv[1]).read_text())\n" + body)
    os.chmod(path, 0o755)
    return path


def command(side, root, args):
    if side == "py":
        return [sys.executable, "-m", "eb_staged_rebuild", *args]
    return [TOOL, *args]


failures = []
cases = 0


def case(name, setup, steps, umask=None, env=None):
    """setup(root) -> (software, modules, state, module); steps are argument lists
    with @S @M @ST @ROOT placeholders."""
    global cases
    cases += 1
    base = tempfile.mkdtemp(prefix="case-", dir=os.path.join(REPO, ".differential-metadata"))
    try:
        results = {}
        for side in ("py", "zig"):
            root = os.path.join(base, side)
            os.makedirs(root)
            software, modules, state, module = setup(root)
            out = []
            for step in steps:
                args = [a.replace("@ST", state).replace("@S", software).replace("@M", modules).replace("@ROOT", root)
                        for a in step]
                full_env = dict(os.environ, EB_STAGED_REBUILD_EB=os.path.join(root, "eb"), PYTHONPATH=REPO,
                                **(env or {}))
                previous = os.umask(umask) if umask is not None else None
                try:
                    done = subprocess.run(command(side, root, args), cwd=root, env=full_env,
                                          capture_output=True, text=True, timeout=300)
                finally:
                    if previous is not None:
                        os.umask(previous)
                out.append((done.returncode, norm(done.stdout, root), norm(done.stderr, root)))
            exists = os.path.exists(module)
            state_of = (open(module, "rb").read().replace(root.encode(), b"<ROOT>"), os.stat(module).st_mode & 0o7777,
                        acl_of(module)) if exists else None
            results[side] = (out, state_of)
        if results["py"] != results["zig"]:
            failures.append(name)
            print(f"FAIL {name}\n  python: {str(results['py'])[:900]}\n  zig:    {str(results['zig'])[:900]}")
        else:
            print(f"ok   {name} (exit {[r[0] for r in results['py'][0]]})")
    finally:
        shutil.rmtree(base, ignore_errors=True)


RUN = ["run", "--software", "@S", "--modules", "@M", "--state-root", "@ST", "--backend", "installpath",
       "--module", "demo/1", "--run-id", "metadata"]


def main():
    os.makedirs(os.path.join(REPO, ".differential-metadata"), exist_ok=True)
    probe = tempfile.mkstemp(dir=os.path.join(REPO, ".differential-metadata"))[1]
    try:
        os.setxattr(probe, POSIX, access_acl())
    except OSError as error:
        print(f"skipped: no POSIX ACLs here ({error})")
        return 0
    finally:
        os.unlink(probe)

    acl_hex = access_acl().hex()
    set_acl = ('for module in context["modules"]:\n'
               f'    os.setxattr(module["prepared"], "system.posix_acl_access", bytes.fromhex("{acl_hex}"))\n')
    to_symlink = ('for module in context["modules"]:\n'
                  '    p = module["prepared"]\n'
                  '    os.rename(p, p + ".real")\n'
                  '    os.symlink(p + ".real", p)\n')

    case("a new module keeps the staged ACL under a restrictive umask",
         lambda root: fixture(root, existing=False), [[*RUN, "--", "demo.eb"]],
         umask=0o077, env={"FAKE_EB_ACL": acl_hex})

    def with_policy(body):
        def setup(root):
            made = fixture(root, existing=True)
            policy(root, body)
            return made
        return setup

    case("the prepare policy's ACL is applied at publication", with_policy(set_acl),
         [[*RUN, "--prepare=@ROOT/policy", "--", "demo.eb"]])
    case("rollback restores the previous mode and the absence of an ACL", with_policy(set_acl),
         [[*RUN, "--prepare=@ROOT/policy", "--", "demo.eb"], ["rollback", "--state-root", "@ST", "metadata"]])
    case("a prepared module replaced by a symlink is refused before publication", with_policy(to_symlink),
         [[*RUN, "--prepare=@ROOT/policy", "--", "demo.eb"]])
    case("a staged ACL on an existing module is published", lambda root: fixture(root, existing=True),
         [[*RUN, "--", "demo.eb"]], env={"FAKE_EB_ACL": acl_hex})

    if failures:
        print(f"{len(failures)} of {cases} case(s) differ")
        return 1
    print(f"all {cases} cases match")
    return 0


if __name__ == "__main__":
    sys.exit(main())

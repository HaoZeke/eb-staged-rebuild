"""Run EasyBuild so it writes a generation that is not the live software directory."""

import os
import shutil
import subprocess
import sys

from eb_staged_rebuild.probe import USER_NAMESPACE_FLAGS

# Options that would send the build at the live tree, or that delete a previous
# install when the module file and the new install directory differ.
_FLAGS = {
    "--bwrap",
    "--disable-bwrap",
    "--remove-ghost-install-dirs",
}
_WITH_VALUE = {
    "--installpath",
    "--installpath-software",
    "--installpath-modules",
    "--prefix",
    "--bwrap-installpath",
    "--bwrap-options",
    "--subdir-software",
    "--subdir-modules",
}


def filter_eb_args(args):
    """Drop install-path and bubblewrap options. Keep the easyconfig and the rest."""
    kept = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            index += 1
            continue
        name = arg.split("=", 1)[0]
        if name in _FLAGS:
            index += 1
            continue
        if name in _WITH_VALUE:
            if "=" not in arg:
                index += 2
            else:
                index += 1
            continue
        kept.append(arg)
        index += 1
    return kept


def ensure_rebuild(args):
    names = {arg.split("=", 1)[0] for arg in args}
    if "--rebuild" in names or "--force" in names or "-f" in names:
        return list(args)
    return list(args) + ["--rebuild"]


def child_env(base, software, modules, live_modules):
    env = dict(base)
    for key in (
        "EASYBUILD_INSTALLPATH",
        "EASYBUILD_PREFIX",
        "EASYBUILD_REMOVE_GHOST_INSTALL_DIRS",
        "EASYBUILD_INSTALLPATH_SOFTWARE",
        "EASYBUILD_INSTALLPATH_MODULES",
    ):
        env.pop(key, None)
    env["EASYBUILD_INSTALLPATH_SOFTWARE"] = os.fspath(software)
    env["EASYBUILD_INSTALLPATH_MODULES"] = os.fspath(modules)
    live = os.fspath(live_modules)
    parts = [part for part in env.get("MODULEPATH", "").split(":") if part]
    if live not in parts:
        env["MODULEPATH"] = ":".join([live, *parts]) if parts else live
    return env


def installpath_dirs(generation_root):
    software = os.path.join(generation_root, "software")
    modules = os.path.join(generation_root, "modules")
    return software, modules


def installpath_command(ctx):
    software, modules = installpath_dirs(ctx.generation_root)
    cmd = [
        ctx.eb_executable,
        f"--installpath-software={software}",
        f"--installpath-modules={modules}",
        *ensure_rebuild(filter_eb_args(ctx.eb_args)),
    ]
    env = child_env(os.environ, software, modules, ctx.modules)
    return cmd, env, software, modules


def bwrap_command(ctx):
    cmd = [
        ctx.eb_executable,
        "--experimental",
        "--bwrap",
        f"--bwrap-installpath={ctx.generation_root}",
        "--bwrap-options=--die-with-parent",
        *ensure_rebuild(filter_eb_args(ctx.eb_args)),
    ]
    env = child_env(os.environ, ctx.software, ctx.modules, ctx.modules)
    return cmd, env


def userns_command(ctx):
    """Run an install-path build with both live trees mounted read-only."""
    inner, env, software, modules = installpath_command(ctx)
    report = (getattr(ctx, "probes", None) or {}).get("userns_sandbox") or {}
    unshare = report.get("path") or shutil.which("unshare") or "unshare"
    command = [
        unshare,
        *USER_NAMESPACE_FLAGS,
        sys.executable,
        "-m",
        "eb_staged_rebuild.userns_sandbox",
        "--read-only",
        ctx.software,
        "--read-only",
        ctx.modules,
        "--",
        *inner,
    ]
    return command, env, software, modules


def dry_run_command(ctx):
    """List module names without installing. Uses the live prefix."""
    cmd = [
        ctx.eb_executable,
        "--dry-run",
        *filter_eb_args(ctx.eb_args),
    ]
    env = child_env(os.environ, ctx.software, ctx.modules, ctx.modules)
    return cmd, env


def run_command(cmd, env, faults, step):
    faults.reach(step)
    completed = subprocess.run(cmd, env=env, check=False)
    return completed.returncode


def format_command(cmd):
    return " ".join(cmd)

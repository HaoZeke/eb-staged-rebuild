"""Run EasyBuild so it writes a generation that is not the live software directory."""

import os
import signal
import subprocess
import threading
import time

from eb_staged_rebuild.errors import Refused

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


def dry_run_command(ctx):
    """List module names without installing. Uses the live prefix."""
    cmd = [
        ctx.eb_executable,
        "--dry-run",
        *filter_eb_args(ctx.eb_args),
    ]
    env = child_env(os.environ, ctx.software, ctx.modules, ctx.modules)
    return cmd, env


def _signal_group(child, signum):
    try:
        os.killpg(child.pid, signum)
    except ProcessLookupError:
        pass


def _group_running(pgid):
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as handle:
                fields = handle.read().rsplit(b")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if int(fields[2]) == pgid and fields[0] not in (b"Z", b"X"):
            return True
    return False


def _kill_group(child):
    """SIGKILL is delivered asynchronously; a member keeps its descriptors until it exits."""
    _signal_group(child, signal.SIGKILL)
    while _group_running(child.pid):
        time.sleep(0.01)


def run_command(cmd, env, faults, step):
    """Keep the caller's lock held until the command process group has stopped.

    Catchable termination signals request group shutdown. The group leader
    remains unreaped until the last group signal, so its identifier cannot be
    reassigned to an unrelated process. Commands must keep their workers in
    their process group and join them before returning.
    """
    faults.reach(step)
    if threading.current_thread() is not threading.main_thread():
        raise Refused("external build and preparation commands require the main thread")
    if not os.path.isfile("/proc/self/stat"):
        raise Refused("external commands require /proc to confirm their process group stopped")
    interrupted = [None]

    def request_stop(signum, frame):
        if interrupted[0] is None:
            interrupted[0] = signum

    signals = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    handlers = {signum: signal.getsignal(signum) for signum in signals}
    child = None
    try:
        for signum in signals:
            signal.signal(signum, request_stop)
        child = subprocess.Popen(cmd, env=env, start_new_session=True)
        while interrupted[0] is None:
            exited = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if exited is not None:
                break
            time.sleep(0.02)
        if interrupted[0] is not None:
            _signal_group(child, signal.SIGTERM)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                time.sleep(min(0.02, max(0, deadline - time.monotonic())))
        # The unreaped leader reserves the process-group identifier. Stray
        # workers must not retain write access beyond command completion.
        _kill_group(child)
        code = child.wait()
        if interrupted[0] is not None:
            return 128 + interrupted[0]
        return code if code >= 0 else 128 - code
    finally:
        try:
            if child is not None and child.returncode is None:
                _kill_group(child)
                child.wait()
        finally:
            for signum, handler in handlers.items():
                signal.signal(signum, handler)


def format_command(cmd):
    return " ".join(cmd)

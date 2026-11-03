"""Crash-aware directory exchange with a restoring two-rename fallback.

The preferred operation is Linux ``renameat2(RENAME_EXCHANGE)``.  Some shared
filesystems reject rename flags, so the fallback uses three ordinary renames.
Every caught failure in that sequence restores the two original names before
it is re-raised.  A journaled caller can use :func:`recover_interrupted_swap`
after an uncatchable process or machine failure.
"""

import ctypes
import errno
import os
from dataclasses import dataclass

from eb_staged_rebuild.atomicio import fsync_dir
from eb_staged_rebuild.errors import NeedsIntervention, Refused
from eb_staged_rebuild.faults import FaultInjector
from eb_staged_rebuild.paths import abspath, lstat_kind


# Linux UAPI: include/uapi/linux/fcntl.h defines AT_FDCWD as -100.
AT_FDCWD = -100
# Linux UAPI: include/uapi/linux/fs.h defines RENAME_EXCHANGE as (1 << 1).
RENAME_EXCHANGE = 1 << 1
FALLBACK_HOLD_SUFFIX = ".fallback-old"
UNSUPPORTED_EXCHANGE_ERRNOS = frozenset(
    {
        errno.EINVAL,
        errno.ENOSYS,
        errno.EOPNOTSUPP,
    }
)


@dataclass(frozen=True)
class SwapResult:
    method: str


def _rename_exchange(first, second):
    """Exchange two paths or raise ``OSError`` with the kernel errno."""
    libc = ctypes.CDLL(None, use_errno=True)
    if not hasattr(libc, "renameat2"):
        raise OSError(errno.ENOSYS, "renameat2 is not exported by libc")
    renameat2 = libc.renameat2
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = renameat2(
        AT_FDCWD,
        os.fsencode(first),
        AT_FDCWD,
        os.fsencode(second),
        RENAME_EXCHANGE,
    )
    if result != 0:
        error = ctypes.get_errno() or errno.EIO
        raise OSError(error, f"renameat2(RENAME_EXCHANGE): {os.strerror(error)}")


def _validate_pair(live, staged):
    live = abspath(live)
    staged = abspath(staged)
    if os.path.dirname(live) != os.path.dirname(staged):
        raise Refused("the live and staged directories must be siblings")
    for label, path in (("live", live), ("staged", staged)):
        kind = lstat_kind(path)
        if kind != "dir":
            raise Refused(f"{label} swap path is {kind}, not a real directory: {path}")
    return live, staged


def _sync_parent(live):
    fsync_dir(os.path.dirname(live))


def _restore_fallback(live, staged, hold, rename):
    """Restore the pre-swap names from either interrupted fallback state."""
    live_kind = lstat_kind(live)
    staged_kind = lstat_kind(staged)
    hold_kind = lstat_kind(hold)
    if hold_kind == "missing":
        return
    if hold_kind != "dir":
        raise NeedsIntervention(f"fallback recovery path is not a directory: {hold}")
    if live_kind == "missing" and staged_kind == "dir":
        # The old live tree was moved aside; the staged tree has not moved.
        rename(hold, live)
    elif live_kind == "dir" and staged_kind == "missing":
        # The staged tree became live; put it back before restoring the old tree.
        rename(live, staged)
        rename(hold, live)
    else:
        raise NeedsIntervention(
            "cannot recover interrupted fallback swap: "
            f"live={live_kind}, staged={staged_kind}, hold={hold_kind}"
        )
    _sync_parent(live)


def recover_interrupted_swap(live, staged, rename=os.rename):
    """Restore an interrupted ordinary-rename fallback, if one is present."""
    live = abspath(live)
    staged = abspath(staged)
    hold = staged + FALLBACK_HOLD_SUFFIX
    _restore_fallback(live, staged, hold, rename)
    if lstat_kind(hold) != "missing":
        raise NeedsIntervention(f"fallback recovery left its hold path behind: {hold}")


def _fallback_swap(live, staged, faults, rename, step_prefix):
    hold = staged + FALLBACK_HOLD_SUFFIX
    if lstat_kind(hold) != "missing":
        recover_interrupted_swap(live, staged, rename)
    try:
        faults.reach(f"{step_prefix}.fallback.live-to-hold")
        rename(live, hold)
        faults.reach(f"{step_prefix}.fallback.staged-to-live")
        rename(staged, live)
        faults.reach(f"{step_prefix}.fallback.hold-to-staged")
        rename(hold, staged)
        _sync_parent(live)
    except BaseException:
        try:
            _restore_fallback(live, staged, hold, rename)
        except BaseException as recovery_error:
            raise NeedsIntervention(
                f"fallback swap failed and could not restore {live}: {recovery_error}"
            ) from recovery_error
        raise
    return SwapResult("rename-fallback")


def swap_directories(
    live,
    staged,
    *,
    allow_exchange=True,
    faults=None,
    rename=os.rename,
    exchange_call=None,
    step_prefix="swap",
):
    """Exchange sibling directories and restore them after every caught failure.

    ``staged`` contains the candidate tree before the call and the retired live
    tree after a successful call.  Unsupported rename flags select the ordinary
    rename fallback.  Any other exchange error leaves both names unchanged.
    """
    live, staged = _validate_pair(live, staged)
    faults = faults if faults is not None else FaultInjector()
    exchange_call = exchange_call or _rename_exchange
    recover_interrupted_swap(live, staged, rename)
    if allow_exchange:
        exchanged = False
        try:
            faults.reach(f"{step_prefix}.exchange.before")
            exchange_call(live, staged)
            exchanged = True
            faults.reach(f"{step_prefix}.exchange.after")
            _sync_parent(live)
            return SwapResult("renameat2-exchange")
        except OSError as error:
            if exchanged:
                try:
                    exchange_call(live, staged)
                    _sync_parent(live)
                except BaseException as recovery_error:
                    raise NeedsIntervention(
                        f"RENAME_EXCHANGE succeeded but its rollback failed for {live}: "
                        f"{recovery_error}"
                    ) from recovery_error
            if error.errno not in UNSUPPORTED_EXCHANGE_ERRNOS:
                raise
        except BaseException:
            if exchanged:
                try:
                    exchange_call(live, staged)
                    _sync_parent(live)
                except BaseException as recovery_error:
                    raise NeedsIntervention(
                        f"RENAME_EXCHANGE succeeded but its rollback failed for {live}: "
                        f"{recovery_error}"
                    ) from recovery_error
            raise
    return _fallback_swap(live, staged, faults, rename, step_prefix)

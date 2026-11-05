"""Remove hidden trees retired by successful swaps or explicit rollbacks.

The clock starts at publish or rollback.  The default grace period is the
unmeasured value carried by ns-051; operators must replace it with the site's
longest job plus a safety margin.  Cluster-wide ``lsof`` is not used.

A module file moved aside by ``retire`` starts that same clock for the stable
software directory the module named. gc reports that directory. It does not
delete it: the directory is the path jobs already have open, and gc only
removes a hidden sibling recorded by a directory swap.
"""

import os
import stat
import time

from eb_staged_rebuild.atomicio import read_bytes
from eb_staged_rebuild.cleanup import remove_generation_tree
from eb_staged_rebuild.constants import SECONDS_PER_HOUR
from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.journal import journal_path, list_journals, save_journal
from eb_staged_rebuild.lockfile import FileLock
from eb_staged_rebuild.modulefile import parse_root
from eb_staged_rebuild.paths import abspath, is_within, lstat_kind
from eb_staged_rebuild.retire import is_retire


def _retirement(journal, mod, include_failed):
    state = journal.get("state")
    if state == "complete":
        return mod.get("published_at"), "previous live tree"
    if state == "rolled_back":
        return journal.get("rolled_back_at"), "rolled-back candidate"
    if include_failed and state == "failed" and mod.get("status") == "pending":
        return journal.get("updated_at") or journal.get("created_at"), "failed candidate"
    return None, f"state {state} is kept"


def _safe_retired_path(mod):
    live = abspath(mod.get("live_software") or "")
    retired = abspath(mod.get("retired") or "")
    if not live or not retired:
        raise Refused("journal has no live/retired software path")
    if live == retired or os.path.dirname(live) != os.path.dirname(retired):
        raise Refused(f"refusing retired path that is not a live sibling: {retired}")
    expected_prefix = f".{os.path.basename(live)}."
    if not os.path.basename(retired).startswith(expected_prefix):
        raise Refused(f"refusing unexpected retired path name: {retired}")
    return retired


def _module_root(path):
    """Return the software directory a tracked module names, or None if it is absent.

    An unreadable, non-regular, or rootless module is a refusal. Guessing that
    nothing refers to a tree is how a collector deletes a directory that is
    still published.
    """
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return None
    except OSError as error:
        raise Refused(f"cannot inspect live module {path}: {error}") from error
    if not stat.S_ISREG(mode):
        raise Refused(f"cannot determine live references from a non-regular module: {path}")
    try:
        _syntax, root = parse_root(read_bytes(path).decode("utf-8"))
    except (OSError, UnicodeError) as error:
        raise Refused(f"cannot read live references from {path}: {error}") from error
    if not root:
        raise Refused(f"cannot determine the live software root from {path}")
    return abspath(root)


def _resolved(path):
    try:
        return os.path.realpath(path, strict=True)
    except OSError as error:
        raise Refused(f"cannot resolve live software references from {path}: {error}") from error


def _live_roots(journals):
    roots = set()
    for journal in journals:
        if is_retire(journal):
            # A retire that is not complete, or that was rolled back, leaves the
            # module file live at its original path.
            if journal.get("state") != "retired":
                root = _module_root(journal["module_path"])
                if root:
                    roots.add(root)
                    roots.add(_resolved(root))
            continue
        for mod in journal.get("modules_plan") or []:
            live = mod.get("live_module")
            if not live:
                continue
            root = _module_root(live)
            if root:
                roots.add(root)
                roots.add(_resolved(root))
    return roots


def _referenced(directory, roots):
    spellings = {abspath(directory), _resolved(directory)}
    return any(is_within(root, spelling) for root in roots for spelling in spellings)


def _iso(when):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))


def _retired_software(journals, roots, grace_hours, now):
    """Report stable software directories named by retired module files.

    gc never deletes these. A hidden previous tree from a directory swap is
    removed by the caller. This entry is ``keep`` while a tracked module still
    resolves inside the directory or the grace period runs, then ``manual``.
    """
    latest = {}
    for journal in journals:
        if not is_retire(journal) or journal.get("state") != "retired":
            continue
        directory = abspath(journal["software_dir"])
        when = journal.get("retired_at")
        if when is None:
            continue
        latest[directory] = max(latest.get(directory, when), when)
    actions = []
    for directory in sorted(latest):
        if not os.path.isdir(directory):
            continue
        ready = latest[directory] + grace_hours * SECONDS_PER_HOUR
        if _referenced(directory, roots):
            actions.append({"path": directory, "action": "keep", "reason": "a live module resolves inside it"})
        elif now < ready:
            actions.append(
                {
                    "path": directory,
                    "action": "keep",
                    "reason": f"within grace (retired module); ready to remove by hand after {_iso(ready)}",
                    "retired_at": latest[directory],
                }
            )
        else:
            actions.append(
                {
                    "path": directory,
                    "action": "manual",
                    "reason": (
                        f"ready to remove by hand after {_iso(ready)}; "
                        "gc does not delete the stable software directory"
                    ),
                    "retired_at": latest[directory],
                }
            )
    return actions


def _collect_locked(state_root, grace_hours, now, dry_run, include_failed):
    state_root = abspath(state_root)
    journals = list_journals(state_root)
    roots = _live_roots(journals)
    actions = []
    for journal in journals:
        if is_retire(journal):
            continue
        changed = False
        for mod in journal.get("modules_plan") or []:
            if mod.get("retired_removed_at"):
                continue
            retired = _safe_retired_path(mod)
            if lstat_kind(retired) != "dir":
                continue
            when, why = _retirement(journal, mod, include_failed)
            if when is None:
                actions.append({"path": retired, "action": "keep", "reason": why})
                continue
            age = now - when
            if _referenced(retired, roots):
                actions.append(
                    {
                        "path": retired,
                        "action": "keep",
                        "reason": "a live module resolves inside it",
                    }
                )
                continue
            if age < grace_hours * SECONDS_PER_HOUR:
                actions.append(
                    {
                        "path": retired,
                        "action": "keep",
                        "reason": f"within grace ({why})",
                        "retired_at": when,
                    }
                )
                continue
            actions.append(
                {
                    "path": retired,
                    "action": "remove",
                    "reason": why,
                    "retired_at": when,
                }
            )
            if dry_run:
                continue
            remove_generation_tree(retired, os.path.dirname(retired))
            mod["retired_removed_at"] = now
            changed = True
        if changed:
            journal["message"] = "retired tree removed"
            save_journal(
                journal_path(state_root, journal["run_id"]),
                journal,
                None,
                "journal.gc",
            )
    actions.extend(_retired_software(journals, roots, grace_hours, now))
    return actions


def collect(
    state_root,
    grace_hours,
    now,
    dry_run=False,
    include_failed=False,
    lock_timeout=None,
):
    """Collect retired trees while holding the same lock as publish/rollback."""
    lock = FileLock(state_root)
    lock.acquire(lock_timeout)
    try:
        return _collect_locked(
            state_root,
            grace_hours,
            now,
            dry_run,
            include_failed,
        )
    finally:
        lock.release()

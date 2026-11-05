"""Remove retired generations. Never a live software directory.

A generation under ``<state-root>/generations/<run-id>`` can be removed when
no live module file resolves inside it and a retirement time is known:

* a later run published a module whose previous root was inside it, or
* this run was rolled back.

The clock starts at that time, not at the moment ``gc`` is invoked. The wait
is ``--grace-hours``. 144 is the figure carried from the staged-rebuild notes
(120 hours plus a day). It is not a measured site walltime. Set it from the
longest job the site will actually run. Cluster-wide ``lsof`` is not used.

This does not search other installations for ``DT_RPATH`` or user scripts that
embed the old path. An unreferenced generation with no retirement record is
kept and reported. Paths outside ``generations/`` are never deleted, including
the original ``software/<name>/<version>`` directory.

A module file moved aside by ``retire`` starts the same clock for the software
directory it named. Inside ``generations/`` that directory is removed with its
generation. Anywhere else it is only reported, as ``manual``, once the grace
period has passed and no tracked module file resolves inside it. gc does not
delete outside ``generations/``.
"""

import os
import time

from eb_staged_rebuild.atomicio import read_bytes
from eb_staged_rebuild.cleanup import remove_generation_tree
from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.journal import journal_path, list_journals, save_journal
from eb_staged_rebuild.lockfile import FileLock
from eb_staged_rebuild.modulefile import parse_root
from eb_staged_rebuild.paths import abspath, is_within, lstat_kind
from eb_staged_rebuild.retire import is_retire


def _live_roots(journals):
    roots = set()
    for journal in journals:
        if is_retire(journal):
            # A retire that is not complete, or that was rolled back, leaves the
            # module file live at its original path.
            if journal.get("state") != "retired" and lstat_kind(journal["module_path"]) == "file":
                try:
                    _syntax, root = parse_root(read_bytes(journal["module_path"]).decode("utf-8"))
                except (OSError, UnicodeError):
                    continue
                if root:
                    roots.add(abspath(root))
            continue
        for mod in journal.get("modules_plan") or []:
            live = mod.get("live_module")
            if not live or lstat_kind(live) != "file":
                continue
            try:
                text = read_bytes(live).decode("utf-8")
            except (OSError, UnicodeError):
                continue
            _syntax, root = parse_root(text)
            if root:
                roots.add(abspath(root))
    return roots


def _referenced(generation, roots):
    generation = abspath(generation)
    for root in roots:
        if root == generation or is_within(root, generation):
            return True
    return False


def _superseded_at(journal, journals):
    generation = abspath(journal["generation_root"])
    times = []
    for other in journals:
        if other.get("run_id") == journal.get("run_id"):
            continue
        if is_retire(other):
            if other.get("state") == "retired" and other.get("retired_at") and is_within(
                abspath(other["software_dir"]), generation
            ):
                times.append(other["retired_at"])
            continue
        for mod in other.get("modules_plan") or []:
            if mod.get("status") != "published":
                continue
            previous = mod.get("previous_root")
            published_at = mod.get("published_at")
            if previous and published_at and is_within(abspath(previous), generation):
                times.append(published_at)
    if not times:
        return None
    return min(times)


def _retirement(journal, journals, include_failed):
    state = journal.get("state")
    if state == "rolled_back":
        return journal.get("rolled_back_at"), "rolled back"
    if state == "complete":
        when = _superseded_at(journal, journals)
        if when is None:
            return None, "unreferenced but no later publish records a retirement time"
        return when, "superseded"
    if include_failed and state == "failed":
        published = [mod for mod in journal.get("modules_plan") or [] if mod.get("status") == "published"]
        if published:
            return None, "failed after a module was published"
        when = journal.get("updated_at") or journal.get("created_at")
        return when, "failed and unpublished"
    return None, f"state {state} is kept for resume"


def collect(state_root, grace_hours, now, dry_run=False, include_failed=False):
    state_root = abspath(state_root)
    lock = FileLock(state_root)
    lock.acquire()
    try:
        return _collect_locked(state_root, grace_hours, now, dry_run, include_failed)
    finally:
        lock.release()


def _collect_locked(state_root, grace_hours, now, dry_run, include_failed):
    generations = os.path.join(state_root, "generations")
    journals = list_journals(state_root)
    roots = _live_roots(journals)
    actions = []
    for journal in journals:
        generation = journal.get("generation_root")
        if is_retire(journal) or not generation:
            continue
        generation = abspath(generation)
        if not is_within(generation, generations):
            raise Refused(f"refusing to consider {generation}, outside {generations}")
        if journal.get("generation_removed_at"):
            continue
        if not os.path.isdir(generation) or os.path.islink(generation):
            continue
        if _referenced(generation, roots):
            actions.append({"path": generation, "action": "keep", "reason": "a live module resolves inside it"})
            continue
        when, why = _retirement(journal, journals, include_failed)
        if when is None:
            actions.append({"path": generation, "action": "keep", "reason": why})
            continue
        age = now - when
        if age < grace_hours * 3600:
            actions.append(
                {
                    "path": generation,
                    "action": "keep",
                    "reason": f"within grace ({why})",
                    "retired_at": when,
                }
            )
            continue
        actions.append({"path": generation, "action": "remove", "reason": why, "retired_at": when})
        if dry_run:
            continue
        remove_generation_tree(generation, generations)
        journal["generation_removed_at"] = now
        journal["message"] = f"generation removed ({why})"
        save_journal(journal_path(state_root, journal["run_id"]), journal, None, "journal.gc")
    actions.extend(_retired_software(journals, roots, generations, grace_hours, now))
    return actions


def _iso(when):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))


def _retired_software(journals, roots, generations, grace_hours, now):
    """Report software directories of retired module files that gc may not delete itself.

    A directory inside ``generations/`` is handled with its generation. The rest
    is never deleted: the entry is ``keep`` while a tracked module still resolves
    inside it or the grace period runs, then ``manual``.
    """
    latest = {}
    for journal in journals:
        if not is_retire(journal) or journal.get("state") != "retired":
            continue
        directory = abspath(journal["software_dir"])
        if is_within(directory, generations):
            continue
        when = journal.get("retired_at")
        if when is None:
            continue
        latest[directory] = max(latest.get(directory, when), when)
    actions = []
    for directory in sorted(latest):
        if not os.path.isdir(directory):
            continue
        ready = latest[directory] + grace_hours * 3600
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
                    "reason": f"ready to remove by hand after {_iso(ready)}; gc deletes only under generations/",
                    "retired_at": latest[directory],
                }
            )
    return actions

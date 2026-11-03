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
"""

import os
import shutil

from eb_staged_rebuild.atomicio import read_bytes
from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.journal import journal_path, list_journals, save_journal
from eb_staged_rebuild.modulefile import parse_root
from eb_staged_rebuild.paths import abspath, is_within, lstat_kind


def _live_roots(journals):
    roots = set()
    for journal in journals:
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
    generations = os.path.join(state_root, "generations")
    journals = list_journals(state_root)
    roots = _live_roots(journals)
    actions = []
    for journal in journals:
        generation = journal.get("generation_root")
        if not generation:
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
        shutil.rmtree(generation)
        journal["generation_removed_at"] = now
        journal["message"] = f"generation removed ({why})"
        save_journal(journal_path(state_root, journal["run_id"]), journal, None, "journal.gc")
    return actions

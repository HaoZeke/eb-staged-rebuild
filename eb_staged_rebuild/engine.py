"""Publish a finished generation by replacing module files, never the software directory.

A crash between steps leaves every live module file either absent (when it did
not exist before this run) or equal to a complete generation's module text.
The software directory at ``<software>/<name>/<version>`` is not renamed.
"""

import json
import os
import shutil
import time
from dataclasses import dataclass, field

from eb_staged_rebuild.atomicio import atomic_write, read_bytes, sha256_bytes
from eb_staged_rebuild.builders import (
    bwrap_command,
    dry_run_command,
    format_command,
    installpath_command,
    installpath_dirs,
    run_command,
)
from eb_staged_rebuild.discover import (
    generation_for,
    map_link_target,
    order_module_files,
    walk_module_tree,
)
from eb_staged_rebuild.errors import NeedsIntervention, Refused
from eb_staged_rebuild.faults import FaultInjector
from eb_staged_rebuild.journal import (
    generation_root as generation_root_for,
    journal_path,
    load_journal,
    run_dir as run_dir_for,
    save_journal,
)
from eb_staged_rebuild.livecheck import empty_real_dir, same_snapshot, snapshot
from eb_staged_rebuild.lockfile import FileLock
from eb_staged_rebuild.modulefile import generation_complete, parse_root
from eb_staged_rebuild.paths import (
    abspath,
    assert_real_components,
    is_real_dir,
    lstat_kind,
)
from eb_staged_rebuild.probe import probe_all, stage_paths_from_bwrap_info

import re

_MODULE_LINE = re.compile(r"^\s*\*\s+\[[ xFR-]\]\s+.*\(module:\s*(.+)\)\s*$")


@dataclass
class Spec:
    software: str
    modules: str
    state_root: str
    eb_args: list
    backend: str = "installpath"
    grace_hours: float = 144.0
    dry_run: bool = False
    module_rels: list = field(default_factory=list)
    eb_executable: str = "eb"
    builder: object = None
    faults: object = None
    run_id: str = None
    skip_live_check: bool = False
    lock_timeout: float = None
    clock: object = None
    probes: dict = None


@dataclass
class Result:
    returncode: int
    run_id: str = None
    state: str = ""
    message: str = ""
    command: str = ""


def _clock(spec):
    return spec.clock if spec.clock is not None else time.time


def _faults(spec):
    return spec.faults if spec.faults is not None else FaultInjector()


def _log(message):
    print(f"eb-staged-rebuild: {message}", flush=True)


class _Ctx:
    def __init__(self, spec, generation_root, faults):
        self.software = spec.software
        self.modules = spec.modules
        self.generation_root = generation_root
        self.eb_executable = spec.eb_executable
        self.eb_args = list(spec.eb_args)
        self.faults = faults
        self.backend = spec.backend


def _check_roots(spec):
    if not is_real_dir(spec.software):
        kind = lstat_kind(spec.software)
        if kind == "symlink":
            raise Refused(
                f"software path is a symlink ({spec.software}); "
                "Name/version must stay a real directory"
            )
        raise Refused(f"software path is not a real directory: {spec.software} ({kind})")
    if not is_real_dir(spec.modules):
        kind = lstat_kind(spec.modules)
        if kind == "symlink":
            raise Refused(f"modules path is a symlink: {spec.modules}")
        raise Refused(f"modules path is not a real directory: {spec.modules} ({kind})")
    if spec.backend not in ("installpath", "bwrap"):
        raise Refused(f"unknown backend {spec.backend}")


def _check_install_dir(software, rel):
    path = os.path.join(software, rel)
    if lstat_kind(path) == "symlink":
        raise Refused(
            f"{path} is a symlink. Name/version must stay a real directory: "
            "bubblewrap 0.12 and later refuse to mount on a symlink, and "
            "bubblewrap 0.6.3 follows one. This tool depends on neither."
        )
    reason = assert_real_components(path)
    if reason:
        raise Refused(f"{rel}: {reason}. Name/version must stay a real directory")
    kind = lstat_kind(path)
    if kind not in ("missing", "dir"):
        raise Refused(f"{path} is a {kind}, not a directory")


def software_rels_from_dry_run(text):
    rels = []
    for line in text.splitlines():
        match = _MODULE_LINE.match(line)
        if not match:
            continue
        field_text = match.group(1).strip()
        if " | " in field_text:
            subdir, short = field_text.split(" | ", 1)
            rels.append(os.path.join(subdir.strip(), short.strip()))
        else:
            rels.append(field_text)
    return rels


def _learn_rels(spec, faults, log):
    rels = []
    for rel in spec.module_rels:
        if rel not in rels:
            rels.append(rel)
    if spec.builder is not None:
        return rels
    ctx = _Ctx(spec, generation_root_for(spec.state_root, "dry-run"), faults)
    cmd, env = dry_run_command(ctx)
    log("module preflight: " + format_command(cmd))
    import subprocess

    completed = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    found = software_rels_from_dry_run((completed.stdout or "") + "\n" + (completed.stderr or ""))
    if completed.returncode != 0 and spec.backend == "bwrap" and not found and not rels:
        tail = (completed.stderr or completed.stdout or "").strip()
        raise Refused(
            "bwrap backend did not get a module list from eb --dry-run, so it will not "
            f"mount on an unknown path (eb exited {completed.returncode}). {tail}"
        )
    for rel in found:
        if rel not in rels:
            rels.append(rel)
    if spec.backend == "bwrap" and not rels:
        raise Refused(
            "bwrap backend needs --module Name/version or an eb --dry-run module line; "
            "refusing to mount on an unknown path"
        )
    return rels


def _new_run_id(spec):
    if spec.run_id:
        return spec.run_id
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(_clock(spec)()))
    base = f"{stamp}-{os.getpid()}"
    candidate = base
    suffix = 0
    while os.path.exists(run_dir_for(spec.state_root, candidate)):
        suffix += 1
        candidate = f"{base}-{suffix}"
    return candidate


def _snapshots(spec, rels):
    if spec.skip_live_check:
        return {}
    recorded = {}
    for rel in rels:
        recorded[rel] = snapshot(os.path.join(spec.software, rel))
    return recorded


def _enforce_manifest(spec, journal):
    if spec.skip_live_check:
        return
    changed = []
    for rel, before in (journal.get("manifests") or {}).items():
        path = os.path.join(spec.software, rel)
        if (
            spec.backend == "bwrap"
            and before.get("exists") is False
            and empty_real_dir(path)
        ):
            try:
                os.rmdir(path)
            except OSError:
                pass
        after = snapshot(path)
        if not same_snapshot(before, after):
            changed.append(rel)
    if changed:
        raise Refused(
            "live software directory changed during the build; not published: "
            + ", ".join(changed)
        )


def _read_bwrap_info(generation_root):
    path = os.path.join(generation_root, "bwrap_info.json")
    if not os.path.isfile(path) or os.path.islink(path):
        raise Refused(f"bwrap backend finished without {path}; nothing was published")
    try:
        info = json.loads(read_bytes(path).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise Refused(f"cannot read {path}: {exc}") from exc
    return info


def _stage_roots(spec, journal, generation_root):
    if spec.backend == "installpath":
        return (
            os.path.join(generation_root, "software"),
            os.path.join(generation_root, "modules"),
            "installpath",
        )
    info = _read_bwrap_info(generation_root)
    version = (journal.get("probes") or {}).get("easybuild", {}).get("framework_version")
    software, modules, how = stage_paths_from_bwrap_info(info, version, generation_root)
    return software, modules, how


def _plan(spec, journal, faults):
    stage_software, stage_modules, how = _stage_roots(spec, journal, journal["generation_root"])
    journal["stage_software"] = abspath(stage_software)
    journal["stage_modules"] = abspath(stage_modules)
    journal["stage_how"] = how
    if not is_real_dir(stage_software):
        raise Refused(f"stage software tree is not a real directory: {stage_software}")
    files, links = walk_module_tree(stage_modules)
    if not files:
        raise Refused(f"no module files under {stage_modules}")
    ordered, texts = order_module_files(files, stage_modules)
    drafted = []
    for path in ordered:
        rel = os.path.relpath(path, stage_modules).replace(os.sep, "/")
        generation, new_text = generation_for(texts[path], stage_software, spec.software)
        if not generation_complete(generation):
            raise Refused(f"{generation} is not a complete generation")
        if os.path.islink(generation):
            raise Refused(f"generation {generation} is a symlink")
        live_software = os.path.join(
            spec.software, os.path.relpath(generation, abspath(stage_software))
        )
        if lstat_kind(live_software) == "symlink":
            raise Refused(
                f"{live_software} is a symlink. Name/version must stay a real directory"
            )
        live_module = os.path.join(spec.modules, rel)
        if lstat_kind(live_module) == "symlink":
            raise Refused(
                f"refusing to replace module symlink {live_module}. "
                "The module file that is the pointer must be a regular file"
            )
        reason = assert_real_components(live_module)
        if reason and lstat_kind(live_module) != "missing":
            raise Refused(reason)
        if lstat_kind(live_module) not in ("missing", "file"):
            raise Refused(f"{live_module} is not a regular file")
        drafted.append((rel, generation, new_text, live_module))

    run_dir = run_dir_for(spec.state_root, journal["run_id"])
    plan = []
    for rel, generation, new_text, live_module in drafted:
        prepared = os.path.join(run_dir, "prepared", rel)
        atomic_write(prepared, new_text, faults, f"prepare:{rel}")
        stamp = {
            "run_id": journal["run_id"],
            "generation": generation,
            "module": rel,
            "sha256": sha256_bytes(new_text.encode("utf-8")),
        }
        atomic_write(
            os.path.join(generation, ".eb-staged-rebuild-complete"),
            json.dumps(stamp, indent=2, sort_keys=True) + "\n",
            faults,
            f"stamp:{rel}",
        )
        existed = lstat_kind(live_module) == "file"
        old_hash = None
        previous_root = None
        if existed:
            old_bytes = read_bytes(live_module)
            old_hash = sha256_bytes(old_bytes)
            _kind, previous_root = parse_root(old_bytes.decode("utf-8"))
        plan.append(
            {
                "rel": rel,
                "live_module": live_module,
                "prepared": prepared,
                "generation": generation,
                "backup": os.path.join(run_dir, "backups", rel),
                "new_hash": sha256_bytes(new_text.encode("utf-8")),
                "old_hash": old_hash,
                "previous_root": previous_root,
                "existed": existed,
                "status": "pending",
                "published_at": None,
            }
        )
    link_plan = []
    for rel, target in links:
        link_plan.append(
            {
                "rel": rel,
                "live_path": os.path.join(spec.modules, rel),
                "target": map_link_target(target, stage_modules, spec.modules),
                "status": "pending",
                "created": False,
            }
        )
    journal["modules_plan"] = plan
    journal["links_plan"] = link_plan


def _publish_one(mod, faults, clock):
    live = mod["live_module"]
    prepared = read_bytes(mod["prepared"])
    if sha256_bytes(prepared) != mod["new_hash"]:
        raise NeedsIntervention(f"prepared module bytes for {mod['rel']} changed")
    if not generation_complete(mod["generation"]):
        raise Refused(f"refusing to publish an incomplete generation {mod['generation']}")
    kind = lstat_kind(live)
    if kind == "symlink":
        raise Refused(f"refusing to replace module symlink {live}")
    if kind == "file":
        current = read_bytes(live)
        current_hash = sha256_bytes(current)
        if current_hash == mod["new_hash"]:
            mod["status"] = "published"
            mod["published_at"] = mod["published_at"] or clock()
            return
        if mod["old_hash"] is None or current_hash != mod["old_hash"]:
            raise NeedsIntervention(
                f"{live} is neither the previous module file nor the one this run prepared"
            )
        backup = mod["backup"]
        if lstat_kind(backup) == "missing":
            atomic_write(backup, current, faults, f"publish.backup:{mod['rel']}")
        elif lstat_kind(backup) != "file" or sha256_bytes(read_bytes(backup)) != mod["old_hash"]:
            raise NeedsIntervention(f"backup for {mod['rel']} does not match the previous module file")
    elif kind == "missing":
        if mod["existed"]:
            raise NeedsIntervention(f"{live} disappeared before it could be published")
    else:
        raise Refused(f"{live} is not a regular file")
    atomic_write(live, prepared, faults, f"publish.file:{mod['rel']}")
    mod["status"] = "published"
    mod["published_at"] = clock()


def _create_links(journal, faults):
    for link in journal.get("links_plan") or []:
        path = link["live_path"]
        target = link["target"]
        kind = lstat_kind(path)
        if kind == "symlink" and os.readlink(path) == target:
            link["status"] = "present"
            continue
        if kind != "missing":
            link["status"] = "kept"
            continue
        parent = os.path.dirname(path)
        reason = assert_real_components(parent)
        if reason:
            raise Refused(reason)
        os.makedirs(parent, exist_ok=True)
        faults.reach(f"links.create:{link['rel']}")
        os.symlink(target, path)
        link["created"] = True
        link["status"] = "created"


def _publish(spec, journal, faults):
    journal["state"] = "publishing"
    save_journal(journal_path(spec.state_root, journal["run_id"]), journal, faults, "journal.publishing")
    clock = _clock(spec)
    for mod in journal["modules_plan"]:
        if mod["status"] == "published":
            continue
        _publish_one(mod, faults, clock)
        _log(f"published {mod['rel']} -> {mod['generation']}")
        save_journal(
            journal_path(spec.state_root, journal["run_id"]),
            journal,
            faults,
            f"journal.module:{mod['rel']}",
        )
    _create_links(journal, faults)
    journal["state"] = "complete"
    journal["completed_at"] = clock()
    journal["message"] = "published"
    save_journal(journal_path(spec.state_root, journal["run_id"]), journal, faults, "journal.complete")


def _clear_stage_modules(generation_root):
    """Drop module files left by an earlier attempt in this generation.

    EasyBuild deletes the install directory named by an existing module file
    when ``remove-ghost-install-dirs`` is on. A bubblewrap module file names
    the live directory. Removing the staged module files before ``eb`` runs
    means a resumed build does not hand EasyBuild that path. A symlink is not
    followed.
    """
    modules = os.path.join(os.path.abspath(generation_root), "modules")
    if os.path.islink(modules):
        raise Refused(f"refusing to clear a symlinked module stage: {modules}")
    if os.path.isdir(modules):
        shutil.rmtree(modules)


def _build(spec, journal, faults):
    journal["state"] = "building"
    save_journal(journal_path(spec.state_root, journal["run_id"]), journal, faults, "journal.building")
    ctx = _Ctx(spec, journal["generation_root"], faults)
    os.makedirs(journal["generation_root"], exist_ok=True)
    _clear_stage_modules(journal["generation_root"])
    faults.reach("build.start")
    if spec.builder is not None:
        rc = spec.builder(ctx)
        journal["eb_command"] = None
    elif spec.backend == "bwrap":
        cmd, env = bwrap_command(ctx)
        journal["eb_command"] = cmd
        _log("running " + format_command(cmd))
        rc = run_command(cmd, env, faults, "build.invoke")
    else:
        software, modules = installpath_dirs(journal["generation_root"])
        os.makedirs(software, exist_ok=True)
        os.makedirs(modules, exist_ok=True)
        cmd, env, _software, _modules = installpath_command(ctx)
        journal["eb_command"] = cmd
        _log("running " + format_command(cmd))
        rc = run_command(cmd, env, faults, "build.invoke")
    faults.reach("build.finish")
    journal["eb_returncode"] = rc
    return rc


def _fail(spec, journal, faults, message, code):
    journal["state"] = "failed"
    journal["message"] = message
    journal["updated_at"] = _clock(spec)()
    save_journal(journal_path(spec.state_root, journal["run_id"]), journal, faults, "journal.failed")
    _log(message)
    return Result(code, journal["run_id"], "failed", message, _command_text(journal))


def _command_text(journal):
    cmd = journal.get("eb_command")
    if not cmd:
        return ""
    return format_command(cmd)


def _run_locked(spec, faults, journal, fresh_build):
    path = journal_path(spec.state_root, journal["run_id"])
    if fresh_build:
        rc = _build(spec, journal, faults)
        if rc != 0:
            return _fail(spec, journal, faults, f"eb exited {rc}; live modules were not changed", rc or 1)
        try:
            _enforce_manifest(spec, journal)
        except Refused as exc:
            return _fail(spec, journal, faults, str(exc), 2)
        try:
            _plan(spec, journal, faults)
        except Refused as exc:
            return _fail(spec, journal, faults, str(exc), 2)
        journal["state"] = "built"
        journal["updated_at"] = _clock(spec)()
        save_journal(path, journal, faults, "journal.built")
    _publish(spec, journal, faults)
    message = f"run {journal['run_id']} published {len(journal['modules_plan'])} module(s)"
    _log(message)
    return Result(0, journal["run_id"], "complete", message, _command_text(journal))


def _blank_journal(spec, run_id, probes, rels):
    now = _clock(spec)()
    return {
        "schema": 1,
        "run_id": run_id,
        "state": "created",
        "backend": spec.backend,
        "software": abspath(spec.software),
        "modules": abspath(spec.modules),
        "state_root": abspath(spec.state_root),
        "generation_root": abspath(generation_root_for(spec.state_root, run_id)),
        "eb_args": list(spec.eb_args),
        "eb_executable": spec.eb_executable,
        "grace_hours": spec.grace_hours,
        "skip_live_check": spec.skip_live_check,
        "module_rels": rels,
        "created_at": now,
        "updated_at": now,
        "manifests": _snapshots(spec, rels),
        "probes": probes,
        "modules_plan": [],
        "links_plan": [],
        "message": "",
        "eb_returncode": None,
        "eb_command": None,
        "retired_at": None,
        "generation_removed_at": None,
    }


def run(spec):
    """Build into a new generation, then publish by replacing module files."""
    _check_roots(spec)
    faults = _faults(spec)
    probes = spec.probes if spec.probes is not None else probe_all(spec.software, spec.eb_executable)
    if spec.dry_run:
        return _dry_run(spec, probes, faults)
    if spec.backend == "bwrap" and not (probes.get("bwrap") or {}).get("usable"):
        reason = (probes.get("bwrap") or {}).get("reason") or "bwrap is not usable"
        raise Refused(f"{reason}; refusing to rebuild in place")
    rels = _learn_rels(spec, faults, _log)
    for rel in rels:
        _check_install_dir(spec.software, rel)
    run_id = _new_run_id(spec)
    lock = FileLock(spec.state_root)
    lock.acquire(spec.lock_timeout)
    try:
        if os.path.exists(journal_path(spec.state_root, run_id)):
            raise Refused(f"run {run_id} already exists; resume it or choose another --run-id")
        os.makedirs(run_dir_for(spec.state_root, run_id), exist_ok=True)
        journal = _blank_journal(spec, run_id, probes, rels)
        save_journal(journal_path(spec.state_root, run_id), journal, faults, "journal.created")
        _log(f"run {run_id} backend {spec.backend} generation {journal['generation_root']}")
        return _run_locked(spec, faults, journal, fresh_build=True)
    finally:
        lock.release()


def _spec_from_journal(journal, builder, faults, clock, lock_timeout):
    return Spec(
        software=journal["software"],
        modules=journal["modules"],
        state_root=journal["state_root"],
        eb_args=list(journal.get("eb_args") or []),
        backend=journal["backend"],
        grace_hours=journal.get("grace_hours") or 144.0,
        module_rels=list(journal.get("module_rels") or []),
        eb_executable=journal.get("eb_executable") or "eb",
        builder=builder,
        faults=faults,
        run_id=journal["run_id"],
        skip_live_check=bool(journal.get("skip_live_check")),
        lock_timeout=lock_timeout,
        clock=clock,
        probes=journal.get("probes"),
    )


def resume(state_root, run_id, builder=None, faults=None, clock=None, lock_timeout=None):
    """Continue a run. A finished run is a no-op. A rolled-back run is refused."""
    path = journal_path(state_root, run_id)
    journal = load_journal(path)
    faults = faults or FaultInjector()
    spec = _spec_from_journal(journal, builder, faults, clock, lock_timeout)
    _check_roots(spec)
    lock = FileLock(spec.state_root)
    lock.acquire(lock_timeout)
    try:
        journal = load_journal(path)
        state = journal["state"]
        published = [mod for mod in journal.get("modules_plan") or [] if mod.get("status") == "published"]
        if state == "complete":
            message = f"run {run_id} is already complete"
            _log(message)
            return Result(0, run_id, state, message, _command_text(journal))
        if state == "rolled_back":
            raise Refused(f"run {run_id} was rolled back; start a new run")
        if state in ("created", "building", "failed"):
            if published:
                raise NeedsIntervention(
                    f"run {run_id} is {state} but a module was already published"
                )
            _log(f"resume {run_id} from {state}: rebuilding the unpublished generation")
            return _run_locked(spec, faults, journal, fresh_build=True)
        if state in ("built", "publishing"):
            _log(f"resume {run_id} from {state}: publishing")
            _publish(spec, journal, faults)
            message = f"run {run_id} published {len(journal['modules_plan'])} module(s)"
            _log(message)
            return Result(0, run_id, "complete", message, _command_text(journal))
        raise NeedsIntervention(f"run {run_id} is in an unknown state {state}")
    finally:
        lock.release()


def _restore_one(mod, faults):
    live = mod["live_module"]
    kind = lstat_kind(live)
    if kind == "symlink":
        raise Refused(f"refusing to roll back through a symlink at {live}")
    if mod.get("status") == "restored":
        return
    if mod["existed"]:
        if kind == "file" and sha256_bytes(read_bytes(live)) == mod["old_hash"]:
            mod["status"] = "restored"
            return
        backup = mod["backup"]
        if lstat_kind(backup) != "file":
            raise NeedsIntervention(f"no backup to restore for {mod['rel']}")
        previous = read_bytes(backup)
        if sha256_bytes(previous) != mod["old_hash"]:
            raise NeedsIntervention(f"backup for {mod['rel']} does not match the recorded module file")
        if kind == "file" and sha256_bytes(read_bytes(live)) != mod["new_hash"]:
            raise NeedsIntervention(
                f"{live} is neither the published module file nor the backed-up one"
            )
        if kind not in ("file", "missing"):
            raise Refused(f"{live} is not a regular file")
        atomic_write(live, previous, faults, f"rollback.file:{mod['rel']}")
        mod["status"] = "restored"
        return
    if kind == "missing":
        mod["status"] = "restored"
        return
    if kind == "file" and sha256_bytes(read_bytes(live)) == mod["new_hash"]:
        faults.reach(f"rollback.remove:{mod['rel']}")
        os.remove(live)
        mod["status"] = "restored"
        return
    raise NeedsIntervention(f"{live} is not the module file this run published")


def rollback(state_root, run_id, faults=None, clock=None, lock_timeout=None):
    """Put back the previous module files. Generations are left on disk."""
    path = journal_path(state_root, run_id)
    faults = faults or FaultInjector()
    now = time.time if clock is None else clock
    lock = FileLock(state_root)
    lock.acquire(lock_timeout)
    try:
        journal = load_journal(path)
        if journal["state"] == "rolled_back" and all(
            mod.get("status") == "restored" for mod in journal.get("modules_plan") or []
        ):
            message = f"run {run_id} is already rolled back"
            _log(message)
            return Result(0, run_id, "rolled_back", message)
        for link in journal.get("links_plan") or []:
            if not link.get("created"):
                continue
            link_path = link["live_path"]
            if lstat_kind(link_path) == "symlink" and os.readlink(link_path) == link["target"]:
                faults.reach(f"rollback.unlink:{link['rel']}")
                os.unlink(link_path)
            link["created"] = False
            link["status"] = "removed"
        for mod in reversed(journal.get("modules_plan") or []):
            _restore_one(mod, faults)
            _log(f"restored {mod['rel']}")
            save_journal(path, journal, faults, f"journal.restore:{mod['rel']}")
        journal["state"] = "rolled_back"
        journal["rolled_back_at"] = now()
        journal["message"] = "rolled back"
        save_journal(path, journal, faults, "journal.rolled_back")
        message = f"run {run_id} rolled back; generation kept at {journal['generation_root']}"
        _log(message)
        return Result(0, run_id, "rolled_back", message)
    finally:
        lock.release()


def _dry_run(spec, probes, faults):
    bwrap = probes.get("bwrap") or {}
    easybuild = probes.get("easybuild") or {}
    exchange = probes.get("rename_exchange") or {}
    lines = [
        f"backend: {spec.backend}",
        f"software: {abspath(spec.software)}",
        f"modules: {abspath(spec.modules)}",
        f"state root: {abspath(spec.state_root)}",
        f"bwrap usable: {bool(bwrap.get('usable'))} ({bwrap.get('reason') or bwrap.get('version') or 'present'})",
        "user.max_user_namespaces: "
        + str((probes.get("user_namespaces") or {}).get("max_user_namespaces")),
        "renameat2 RENAME_EXCHANGE on "
        + str(exchange.get("fstype") or "this filesystem")
        + ": "
        + ("yes" if exchange.get("ok") else "no"),
        "easybuild: " + (easybuild.get("version_text") or easybuild.get("reason") or "not found"),
        "flock cross-node: unmeasured (open-004)",
        "publish: replace each module file in place; do not rename the software directory",
    ]
    problems = []
    if spec.backend == "bwrap" and not bwrap.get("usable"):
        problems.append(bwrap.get("reason") or "bwrap is not usable")
    rels = list(spec.module_rels)
    for rel in rels:
        path = os.path.join(spec.software, rel)
        if lstat_kind(path) == "symlink":
            problems.append(f"{path} is a symlink")
    ctx = _Ctx(spec, os.path.join(spec.state_root, "generations", "<run-id>"), faults)
    if spec.backend == "bwrap":
        cmd, _env = bwrap_command(ctx)
    else:
        cmd, _env, _software, _modules = installpath_command(ctx)
    lines.append("command: " + format_command(cmd))
    if problems:
        message = "dry-run would refuse: " + "; ".join(problems)
        _log(message)
        for line in lines:
            _log(line)
        return Result(2, None, "dry-run", message, format_command(cmd))
    message = "dry-run: no build, no module file written"
    _log(message)
    for line in lines:
        _log(line)
    return Result(0, None, "dry-run", message, format_command(cmd))


def describe_module(path):
    kind = lstat_kind(path)
    if kind != "file":
        return {"path": os.fspath(path), "kind": kind, "complete": False, "root": None}
    text = read_bytes(path).decode("utf-8")
    syntax, root = parse_root(text)
    return {
        "path": os.fspath(path),
        "kind": "file",
        "syntax": syntax,
        "root": root,
        "root_kind": lstat_kind(root) if root else None,
        "complete": bool(root) and generation_complete(root),
    }

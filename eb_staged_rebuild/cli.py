"""Command line for eb-staged-rebuild."""

import argparse
import json
import os
import sys

from eb_staged_rebuild import __version__
from eb_staged_rebuild.constants import DEFAULT_GRACE_HOURS
from eb_staged_rebuild.engine import Spec, describe_module, resume, rollback, run
from eb_staged_rebuild.errors import NeedsIntervention, Refused, StagedRebuildError
from eb_staged_rebuild.gc import collect
from eb_staged_rebuild.journal import list_journals
from eb_staged_rebuild.retire import retire
from eb_staged_rebuild.probe import probe_all


def _software(value):
    return value or os.environ.get("EASYBUILD_INSTALLPATH_SOFTWARE")


def _modules(value):
    return value or os.environ.get("EASYBUILD_INSTALLPATH_MODULES")


def _state_root(value, software):
    if value:
        return value
    env = os.environ.get("EB_STAGED_REBUILD_STATE_ROOT")
    if env:
        return env
    if not software:
        return None
    return os.path.join(os.path.dirname(os.path.abspath(software)), ".eb-staged-rebuild")


def _grace(value):
    if value is not None:
        return value
    return float(
        os.environ.get(
            "EB_STAGED_REBUILD_GRACE_HOURS",
            str(DEFAULT_GRACE_HOURS),
        )
    )


def _add_common(parser):
    parser.add_argument("--software", help="live EASYBUILD_INSTALLPATH_SOFTWARE")
    parser.add_argument("--modules", help="live EASYBUILD_INSTALLPATH_MODULES")
    parser.add_argument("--state-root", help="journal and generation root (default: <parent of software>/.eb-staged-rebuild)")
    parser.add_argument("--eb", default=os.environ.get("EB_STAGED_REBUILD_EB", "eb"), help="EasyBuild executable")


def _parser():
    parser = argparse.ArgumentParser(
        prog="eb-staged-rebuild",
        description=(
            "Build EasyBuild software in a staged tree, then exchange the complete "
            "tree with the stable live directory."
        ),
    )
    parser.add_argument("--version", action="version", version=f"eb-staged-rebuild {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    probe = sub.add_parser("probe", help="report bwrap, user namespaces, RENAME_EXCHANGE, and EasyBuild")
    probe.add_argument("--path", help="directory on the filesystem to probe (default: .)")
    probe.add_argument("--eb", default=os.environ.get("EB_STAGED_REBUILD_EB", "eb"))
    probe.add_argument("--json", action="store_true")

    run_p = sub.add_parser("run", help="build and atomically swap a staged tree")
    _add_common(run_p)
    run_p.add_argument(
        "--backend",
        choices=("auto", "bwrap", "userns", "installpath"),
        default="auto",
        help=(
            "build isolation: auto tries bwrap, then a read-only user namespace, "
            "then the unsandboxed install-path fallback"
        ),
    )
    run_p.add_argument("--grace-hours", type=float, default=None)
    run_p.add_argument("--dry-run", action="store_true")
    run_p.add_argument("--module", action="append", default=[], dest="module_rels", metavar="NAME/VERSION")
    run_p.add_argument("--run-id", default=None)
    run_p.add_argument("--lock-timeout", type=float, default=None)
    run_p.add_argument("--skip-live-check", action="store_true")
    run_p.add_argument("eb_args", nargs=argparse.REMAINDER, help="easyconfig and eb options, after --")

    resume_p = sub.add_parser("resume", help="continue a run after a crash")
    resume_p.add_argument("--state-root", required=True)
    resume_p.add_argument("--lock-timeout", type=float, default=None)
    resume_p.add_argument("run_id")

    back_p = sub.add_parser("rollback", help="restore the previous module files, or put a retired module file back")
    back_p.add_argument("--state-root", required=True)
    back_p.add_argument("--lock-timeout", type=float, default=None)
    back_p.add_argument("run_id")

    gc_p = sub.add_parser("gc", help="remove retired generations after the grace period")
    gc_p.add_argument("--state-root", required=True)
    gc_p.add_argument("--grace-hours", type=float, default=None)
    gc_p.add_argument("--dry-run", action="store_true")
    gc_p.add_argument(
        "--include-failed",
        action="store_true",
        help="also remove failed runs that published nothing and are older than the grace period",
    )

    retire_p = sub.add_parser(
        "retire",
        help="move a module file out of the module tree after its package is published elsewhere",
    )
    retire_p.add_argument("--state-root", required=True)
    retire_p.add_argument("--module-file", required=True, help="module file to retire")
    retire_p.add_argument("--reason", default=None)
    retire_p.add_argument("--dry-run", action="store_true")
    retire_p.add_argument("--lock-timeout", type=float, default=0.0, help="seconds to wait for the lock (default: refuse at once)")

    status_p = sub.add_parser("status", help="show run journals")
    status_p.add_argument("--state-root", required=True)
    status_p.add_argument("run_id", nargs="?")

    resolve_p = sub.add_parser("resolve", help="print the generation a module file points at")
    resolve_p.add_argument("module")
    return parser


def _eb_args(raw):
    args = list(raw)
    if args and args[0] == "--":
        args = args[1:]
    if not any(not arg.startswith("-") for arg in args):
        raise Refused("pass the easyconfig after --, for example: eb-staged-rebuild run --software S --modules M -- file.eb")
    return args


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        code = _dispatch(args)
    except (Refused, NeedsIntervention, StagedRebuildError) as exc:
        print(f"eb-staged-rebuild: {exc}", file=sys.stderr)
        return 2
    return code


def _dispatch(args):
    if args.command == "probe":
        directory = args.path or os.getcwd()
        report = probe_all(directory, args.eb)
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            _print_probe(report)
        return 0
    if args.command == "resolve":
        described = describe_module(args.module)
        print(json.dumps(described, indent=2, sort_keys=True))
        if described.get("kind") != "file" or not described.get("complete"):
            return 1
        return 0
    if args.command == "run":
        software = _software(args.software)
        modules = _modules(args.modules)
        if not software or not modules:
            raise Refused("--software and --modules (or EASYBUILD_INSTALLPATH_SOFTWARE and EASYBUILD_INSTALLPATH_MODULES) are required")
        spec = Spec(
            software=software,
            modules=modules,
            state_root=_state_root(args.state_root, software),
            eb_args=_eb_args(args.eb_args),
            backend=args.backend,
            grace_hours=_grace(args.grace_hours),
            dry_run=args.dry_run,
            module_rels=list(args.module_rels),
            eb_executable=args.eb,
            run_id=args.run_id,
            skip_live_check=args.skip_live_check,
            lock_timeout=args.lock_timeout,
        )
        result = run(spec)
        return result.returncode
    if args.command == "resume":
        result = resume(args.state_root, args.run_id, lock_timeout=args.lock_timeout)
        return result.returncode
    if args.command == "rollback":
        result = rollback(args.state_root, args.run_id, lock_timeout=args.lock_timeout)
        return result.returncode
    if args.command == "gc":
        actions = collect(
            args.state_root,
            _grace(args.grace_hours),
            __import__("time").time(),
            dry_run=args.dry_run,
            include_failed=args.include_failed,
        )
        for action in actions:
            print(f"{action['action']}: {action['path']} ({action['reason']})")
        return 0
    if args.command == "retire":
        result = retire(
            args.state_root,
            args.module_file,
            reason=args.reason,
            dry_run=args.dry_run,
            lock_timeout=args.lock_timeout,
        )
        return result.returncode
    if args.command == "status":
        journals = list_journals(args.state_root)
        if args.run_id:
            journals = [journal for journal in journals if journal.get("run_id") == args.run_id]
            if not journals:
                raise Refused(f"no journal for {args.run_id}")
        if not journals:
            print("no runs")
            return 0
        for journal in journals:
            if journal.get("kind") == "retire":
                print(f"{journal.get('run_id')}  {journal.get('state')}  retire  {journal.get('module_path')}")
                print(f"  software directory {journal.get('software_dir')}")
                continue
            modules = journal.get("modules_plan") or []
            published = sum(1 for mod in modules if mod.get("status") == "published")
            print(
                f"{journal.get('run_id')}  {journal.get('state')}  "
                f"backend={journal.get('backend')}  modules={published}/{len(modules)}"
            )
            if journal.get("message"):
                print(f"  {journal['message']}")
            for mod in modules:
                print(
                    f"  {mod.get('status'):<10} {mod.get('rel')}  "
                    f"live={mod.get('live_software')} retired={mod.get('retired')}"
                )
        return 0
    raise Refused(f"unknown command {args.command}")


def _print_probe(report):
    bwrap = report["bwrap"]
    namespaces = report["user_namespaces"]
    userns = report["userns_sandbox"]
    exchange = report["rename_exchange"]
    easybuild = report["easybuild"]
    filesystem = report.get("filesystem") or {}
    print(f"bwrap: {'usable' if bwrap.get('usable') else 'not usable'}")
    if bwrap.get("version"):
        print(f"  version: {bwrap['version']}")
    if bwrap.get("reason"):
        print(f"  reason: {bwrap['reason']}")
    print(f"user.max_user_namespaces: {namespaces.get('max_user_namespaces')}")
    if namespaces.get("unprivileged_userns_clone") is not None:
        print(f"kernel.unprivileged_userns_clone: {namespaces['unprivileged_userns_clone']}")
    print(f"user-namespace sandbox: {'usable' if userns.get('usable') else 'not usable'}")
    if userns.get("reason"):
        print(f"  reason: {userns['reason']}")
    fstype = exchange.get("fstype") or filesystem.get("fstype")
    print(
        "RENAME_EXCHANGE: "
        + ("yes" if exchange.get("ok") else "no")
        + (f" on {fstype}" if fstype else "")
    )
    if exchange.get("error"):
        print(f"  {exchange['error']}")
    if easybuild.get("found"):
        version = easybuild.get("framework_version")
        rendered = ".".join(str(part) for part in version) if version else "unknown"
        print(f"EasyBuild framework: {rendered}")
        print(f"  bwrap stage-path keys: {easybuild.get('stage_path_keys')} ({easybuild.get('keys_source')})")
    else:
        print(f"EasyBuild: {easybuild.get('reason')}")
    print(f"filesystem: {filesystem.get('fstype')} {filesystem.get('mount') or ''}".rstrip())
    print("flock across nodes: unmeasured (open-004)")


if __name__ == "__main__":
    raise SystemExit(main())

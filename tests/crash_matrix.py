"""Kill a run at every step it reaches, resume it, and compare the outcome.

For each fault step a run of the Python tool reaches, the Python tool and
the program named by TOOL_BIN each run on a fresh copy of one fixture, are
paused at that step (EB_STAGED_REBUILD_PAUSE_AT), killed with SIGKILL, and
resumed. Both must leave the same live tree, the same module file and the
same journal state; a run must end either with the old tree untouched or
fully published. The same is done for rollback of a complete run.

Five fixtures: one module; two modules (lib/1 and app/1); one module
whose build leaves the installation and module tree read-only, which is
published with its modes and is not built again once the build was
journalled; one module with
a --prepare policy that counts its invocations, which must run once,
or twice when the kill falls after it exited and before the journal
recorded it; and one module forced onto the
ordinary-rename fallback. Run and rollback take the swap method from the
journal's recorded rename_exchange probe, so the fallback fixture stops a
run at build.start, records the probe as failed, and kills the resumed
run or a later rollback at each fallback rename.

    TOOL_BIN=zig-out/bin/eb-staged-rebuild python3 tests/crash_matrix.py
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE_EB = os.path.join(REPO, "tests", "fake_eb.py")
sys.path.insert(0, REPO)

from eb_staged_rebuild.engine import Spec, rollback, run  # noqa: E402
from eb_staged_rebuild.faults import FaultInjector  # noqa: E402

FALLBACK = ("live-to-hold", "staged-to-live", "hold-to-staged")


class Fixture:
    def __init__(self, name, rels, prepare=False, seal=False):
        self.name = name
        self.rels = rels
        self.prepare = prepare
        self.seal = seal

    def build(self, root):
        for rel in self.rels:
            name, version = rel.split("/")
            os.makedirs(os.path.join(root, "modules", "all", name))
            os.makedirs(os.path.join(root, "software", name, version, "easybuild"))
            with open(os.path.join(root, "software", name, version, "easybuild", f"{name}-{version}.eb"), "w") as handle:
                handle.write(f"name = '{name}'\n")
            with open(os.path.join(root, "software", name, version, "PAYLOAD"), "w") as handle:
                handle.write("old\n")
            with open(os.path.join(root, "modules", "all", name, f"{version}.lua"), "w") as handle:
                handle.write(f'local root = "{root}/software/{rel}"\nsetenv("EBROOT{name.upper()}", root)\n')
        os.makedirs(os.path.join(root, "state"))
        eb = os.path.join(root, "eb")
        with open(eb, "w") as handle:
            handle.write(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_EB}" "$@"\n')
        os.chmod(eb, 0o755)
        if self.prepare:
            policy = os.path.join(root, "prepare")
            with open(policy, "w") as handle:
                handle.write(f'#!/bin/sh\necho x >> "{root}/prepare-count"\n')
            os.chmod(policy, 0o755)

    def env(self, root):
        env = {"FAKE_EB_RELS": ",".join(self.rels), "FAKE_EB_ARGV": os.path.join(root, "eb-argv")}
        if self.seal:
            env["FAKE_EB_SEAL"] = "1"
        return env

    def run_args(self, root, run_id):
        args = [
            "run", "--software", f"{root}/software", "--modules", f"{root}/modules",
            "--state-root", f"{root}/state", "--backend", "installpath", "--eb", f"{root}/eb",
            "--run-id", run_id,
        ]
        if self.prepare:
            args += ["--prepare", f"{root}/prepare"]
        for rel in self.rels:
            args += ["--module", rel]
        return args + ["--", "demo-1.eb"]

    def spec(self, root, faults=None):
        return Spec(
            software=f"{root}/software", modules=f"{root}/modules", state_root=f"{root}/state",
            eb_args=["demo-1.eb"], backend="installpath", eb_executable=f"{root}/eb",
            module_rels=list(self.rels), run_id="r1", faults=faults,
            prepare_executable=f"{root}/prepare" if self.prepare else None,
        )


ONE = Fixture("one module", ["demo/1"])
TWO = Fixture("two modules", ["lib/1", "app/1"])
PREPARE = Fixture("prepare", ["demo/1"], prepare=True)
SEALED = Fixture("sealed", ["demo/1"], seal=True)


def unseal(root):
    """Make every directory writable again, so the tree can be removed."""
    for directory, dirs, _files in os.walk(root):
        for name in dirs:
            path = os.path.join(directory, name)
            if not os.path.islink(path):
                os.chmod(path, 0o755)
    if os.path.isdir(root):
        os.chmod(root, 0o755)


def remove(root):
    unseal(root)
    shutil.rmtree(root, ignore_errors=True)


def modes(top):
    """Mode of every path under `top`, relative, without the tool's stamp."""
    found = {".": oct(os.lstat(top).st_mode & 0o7777)}
    for directory, dirs, files in os.walk(top):
        for name in dirs + files:
            if name == ".eb-staged-rebuild-complete":
                continue
            path = os.path.join(directory, name)
            found[os.path.relpath(path, top)] = oct(os.lstat(path).st_mode & 0o7777)
    return found


def builds(root):
    """Builds the fake eb ran, not counting the preflight or a version probe."""
    path = os.path.join(root, "eb-argv")
    if not os.path.exists(path):
        return 0
    with open(path) as handle:
        return sum(1 for line in handle if any(arg.startswith("--installpath-software=") for arg in json.loads(line)))


def with_env(extra, fn):
    saved = {key: os.environ.get(key) for key in extra}
    os.environ.update(extra)
    try:
        return fn()
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def steps_reached(fixture):
    """Every fault step one Python run reaches, in order."""
    root = tempfile.mkdtemp(prefix="crash-steps-")
    try:
        fixture.build(root)
        faults = FaultInjector()
        result = with_env(fixture.env(root), lambda: run(fixture.spec(root, faults)))
        assert result.returncode == 0, result
        return list(dict.fromkeys(faults.reached))
    finally:
        remove(root)


def steps_rollback(fixture):
    """Every fault step one Python rollback reaches."""
    root = tempfile.mkdtemp(prefix="crash-rbsteps-")
    try:
        fixture.build(root)
        assert with_env(fixture.env(root), lambda: run(fixture.spec(root))).returncode == 0
        faults = FaultInjector()
        assert rollback(f"{root}/state", "r1", faults=faults).returncode == 0
        return list(dict.fromkeys(faults.reached))
    finally:
        remove(root)


def command(impl):
    if impl == "py":
        return [sys.executable, "-m", "eb_staged_rebuild"]
    return [os.environ["TOOL_BIN"]]


def env_for(fixture, root, step, ready):
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO
    env.update(fixture.env(root))
    if step:
        env["EB_STAGED_REBUILD_PAUSE_AT"] = step
        env["EB_STAGED_REBUILD_PAUSE_READY"] = ready
        env["EB_STAGED_REBUILD_PAUSE_SECONDS"] = "30"
    return env


def kill_at(impl, fixture, args, step, root):
    """Start the command, wait until it pauses at `step`, then SIGKILL it."""
    ready = os.path.join(root, "ready")
    proc = subprocess.Popen(
        command(impl) + args, env=env_for(fixture, root, step, ready), cwd=root,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    deadline = time.monotonic() + 60
    while not os.path.exists(ready):
        if proc.poll() is not None:
            return f"exited {proc.returncode} before the pause"
        if time.monotonic() > deadline:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            return "never paused"
        time.sleep(0.02)
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()
    os.unlink(ready)
    return "killed"


def finish(impl, fixture, args, root):
    return subprocess.run(
        command(impl) + args, env=env_for(fixture, root, None, ""), cwd=root, capture_output=True, text=True,
    ).returncode


def journal_file(root, run_id):
    return os.path.join(root, "state", "runs", run_id, "journal.json")


def without_exchange(root, run_id):
    """Record the rename_exchange probe as failed, so the swap uses the fallback."""
    path = journal_file(root, run_id)
    with open(path) as handle:
        journal = json.load(handle)
    journal["probes"]["rename_exchange"]["ok"] = False
    with open(path, "w") as handle:
        json.dump(journal, handle, indent=2, sort_keys=True)
        handle.write("\n")


def outcome(fixture, root, run_id):
    payloads = []
    modules = []
    for rel in fixture.rels:
        name, version = rel.split("/")
        with open(os.path.join(root, "software", name, version, "PAYLOAD")) as handle:
            payloads.append(handle.read())
        with open(os.path.join(root, "modules", "all", name, f"{version}.lua")) as handle:
            modules.append(handle.read().replace(root, "W"))
    state = methods = None
    if os.path.exists(journal_file(root, run_id)):
        with open(journal_file(root, run_id)) as handle:
            journal = json.load(handle)
        plan = journal.get("modules_plan") or []
        state = (journal["state"], [mod.get("status") for mod in plan])
        methods = [mod.get("swap_method") for mod in plan]
    hidden = sorted(
        name
        for rel in fixture.rels
        for name in os.listdir(os.path.join(root, "software", rel.split("/")[0]))
        if name.startswith(".")
    )
    info = {"payloads": payloads, "modules": modules, "state": state, "methods": methods, "hidden": hidden}
    if fixture.seal:
        info["builds"] = builds(root)
        info["live_modes"] = [modes(os.path.join(root, "software", rel)) for rel in fixture.rels]
    count = os.path.join(root, "prepare-count")
    if fixture.prepare:
        info["prepare_runs"] = open(count).read().count("x") if os.path.exists(count) else 0
    return info


def compare(label, fixture, results, kind, must_pause=False, method=None, prepare_runs=1, one_build=False):
    if results["py"] != results["zig"]:
        print(f"DIFF {label}")
        for impl in ("py", "zig"):
            print(f"     {impl}: {results[impl]}")
        return 1
    final = results["py"]
    published = ("complete", ["published"] * len(fixture.rels))
    if kind == "run":
        # Published in full, or untouched when no journal was written.
        ok = (final["state"] == published and set(final["payloads"]) == {"from-eb\n"}) or (
            final["state"] is None and set(final["payloads"]) == {"old\n"}
        )
        # The policy runs once, or twice when the kill fell after it exited
        # and before the journal recorded it as complete.
        if ok and fixture.prepare and final["state"] == published:
            ok = final["prepare_runs"] == prepare_runs
        # A sealed installation is published as the build left it, and a
        # resume after the build was journalled does not build again.
        if ok and fixture.seal and final["state"] == published:
            sealed = {mode for tree in final["live_modes"] for mode in tree.values()}
            ok = sealed <= {"0o555", "0o444"} and (final["builds"] == 1 or not one_build)
    else:
        ok = final["state"] == ("rolled_back", ["restored"] * len(fixture.rels)) and set(final["payloads"]) == {"old\n"}
    if ok and must_pause:
        ok = final["kill"] == "killed"
    if ok and method:
        ok = final["methods"] == [method] * len(fixture.rels)
    if not ok:
        print(f"BAD  {label}: {final}")
        return 1
    print(f"same {label}: {final['state']} payloads {[p.strip() for p in final['payloads']]}")
    return 0


def each_impl(prefix, body):
    results = {}
    for impl in ("py", "zig"):
        root = tempfile.mkdtemp(prefix=f"{prefix}-{impl}-")
        try:
            results[impl] = body(impl, root)
        finally:
            remove(root)
    return results


def run_matrix(fixture):
    failures = 0
    steps = steps_reached(fixture)
    # The policy's success is durable once journal.prepared is renamed into
    # place; a kill after prepare.invoke and up to that rename reruns it.
    rerun = set()
    built = set(steps[steps.index("journal.built.rename") + 1:])
    if fixture.prepare:
        start = steps.index("prepare.invoke") + 1
        rerun = set(steps[start:steps.index("journal.prepared.rename", start) + 1])
    for step in steps:
        def body(impl, root):
            fixture.build(root)
            how = kill_at(impl, fixture, fixture.run_args(root, "r1"), step, root)
            rc = finish(impl, fixture, ["resume", "--state-root", f"{root}/state", "r1"], root)
            return {"kill": how, "resume_rc": rc, **outcome(fixture, root, "r1")}
        failures += compare(
            f"{fixture.name}: run killed at {step}", fixture, each_impl("crash", body), "run",
            prepare_runs=2 if step in rerun else 1, one_build=step in built,
        )
    rollback_steps = steps_rollback(fixture)
    for step in rollback_steps:
        def body(impl, root):
            fixture.build(root)
            assert finish(impl, fixture, fixture.run_args(root, "r1"), root) == 0
            how = kill_at(impl, fixture, ["rollback", "--state-root", f"{root}/state", "r1"], step, root)
            rc = finish(impl, fixture, ["rollback", "--state-root", f"{root}/state", "r1"], root)
            return {"kill": how, "rollback_rc": rc, **outcome(fixture, root, "r1")}
        failures += compare(f"{fixture.name}: rollback killed at {step}", fixture, each_impl("crash-rb", body), "rollback")
    return failures, len(steps) + len(rollback_steps)


def fallback_matrix():
    """The ordinary-rename fallback, killed after each of its renames."""
    fixture = ONE
    failures = total = 0

    def published_by_fallback(impl, root):
        fixture.build(root)
        assert kill_at(impl, fixture, fixture.run_args(root, "r1"), "build.start", root) == "killed"
        without_exchange(root, "r1")

    def complete(impl, root):
        published_by_fallback(impl, root)
        rc = finish(impl, fixture, ["resume", "--state-root", f"{root}/state", "r1"], root)
        return {"kill": "killed", "resume_rc": rc, **outcome(fixture, root, "r1")}

    failures += compare("fallback: run published", fixture, each_impl("crash-fb", complete), "run", method="rename-fallback")
    total += 1
    for rel in fixture.rels:
        for name in FALLBACK:
            step = f"swap:all/{rel}.lua.fallback.{name}"

            def body(impl, root):
                published_by_fallback(impl, root)
                how = kill_at(impl, fixture, ["resume", "--state-root", f"{root}/state", "r1"], step, root)
                rc = finish(impl, fixture, ["resume", "--state-root", f"{root}/state", "r1"], root)
                return {"kill": how, "resume_rc": rc, **outcome(fixture, root, "r1")}
            failures += compare(f"fallback: run killed at {step}", fixture, each_impl("crash-fb", body), "run", must_pause=True, method="rename-fallback")
            total += 1

            rb_step = f"rollback.{step}"

            def rb_body(impl, root):
                published_by_fallback(impl, root)
                assert finish(impl, fixture, ["resume", "--state-root", f"{root}/state", "r1"], root) == 0
                how = kill_at(impl, fixture, ["rollback", "--state-root", f"{root}/state", "r1"], rb_step, root)
                rc = finish(impl, fixture, ["rollback", "--state-root", f"{root}/state", "r1"], root)
                return {"kill": how, "rollback_rc": rc, **outcome(fixture, root, "r1")}
            failures += compare(f"fallback: rollback killed at {rb_step}", fixture, each_impl("crash-fb", rb_body), "rollback", must_pause=True)
            total += 1
    return failures, total


def main():
    if not os.environ.get("TOOL_BIN"):
        print("set TOOL_BIN", file=sys.stderr)
        return 2
    failures = total = 0
    for fixture in (ONE, TWO, PREPARE, SEALED):
        f, t = run_matrix(fixture)
        failures, total = failures + f, total + t
    f, t = fallback_matrix()
    failures, total = failures + f, total + t
    print(f"{total - failures} of {total} cases match")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

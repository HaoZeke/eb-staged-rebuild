"""Kill a run at every step it reaches, resume it, and compare the outcome.

For each fault step a run of the Python tool reaches, the Python tool and
the program named by TOOL_BIN each run on a fresh copy of one fixture, are
paused at that step (EB_STAGED_REBUILD_PAUSE_AT), killed with SIGKILL, and
resumed. Both must leave the same live tree, the same module file and the
same journal state; a run must end either with the old tree untouched or
fully published. The same is done for rollback of a complete run.

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

from eb_staged_rebuild.engine import Spec, run  # noqa: E402
from eb_staged_rebuild.faults import FaultInjector  # noqa: E402


def fixture(root):
    os.makedirs(os.path.join(root, "modules", "all", "demo"))
    os.makedirs(os.path.join(root, "software", "demo", "1", "easybuild"))
    os.makedirs(os.path.join(root, "state"))
    with open(os.path.join(root, "software", "demo", "1", "easybuild", "demo-1.eb"), "w") as handle:
        handle.write("name = 'demo'\n")
    with open(os.path.join(root, "software", "demo", "1", "PAYLOAD"), "w") as handle:
        handle.write("old\n")
    with open(os.path.join(root, "modules", "all", "demo", "1.lua"), "w") as handle:
        handle.write(f'local root = "{root}/software/demo/1"\nsetenv("EBROOTDEMO", root)\n')
    eb = os.path.join(root, "eb")
    with open(eb, "w") as handle:
        handle.write(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_EB}" "$@"\n')
    os.chmod(eb, 0o755)


def run_args(root, run_id):
    return [
        "run", "--software", f"{root}/software", "--modules", f"{root}/modules",
        "--state-root", f"{root}/state", "--backend", "installpath", "--eb", f"{root}/eb",
        "--run-id", run_id, "--module", "demo/1", "--", "demo-1.eb",
    ]


def steps_reached():
    """Every fault step one Python run reaches, in order."""
    root = tempfile.mkdtemp(prefix="crash-steps-")
    try:
        fixture(root)
        faults = FaultInjector()
        spec = Spec(
            software=f"{root}/software", modules=f"{root}/modules", state_root=f"{root}/state",
            eb_args=["demo-1.eb"], backend="installpath", eb_executable=f"{root}/eb",
            module_rels=["demo/1"], run_id="r1", faults=faults,
        )
        result = run(spec)
        assert result.returncode == 0, result
        return list(dict.fromkeys(faults.reached))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def command(impl):
    if impl == "py":
        return [sys.executable, "-m", "eb_staged_rebuild"]
    return [os.environ["TOOL_BIN"]]


def env_for(step, ready):
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO
    if step:
        env["EB_STAGED_REBUILD_PAUSE_AT"] = step
        env["EB_STAGED_REBUILD_PAUSE_READY"] = ready
        env["EB_STAGED_REBUILD_PAUSE_SECONDS"] = "30"
    return env


def kill_at(impl, args, step, root):
    """Start the command, wait until it pauses at `step`, then SIGKILL it."""
    ready = os.path.join(root, "ready")
    proc = subprocess.Popen(
        command(impl) + args, env=env_for(step, ready), cwd=root,
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


def outcome(impl, root, run_id):
    resumed = subprocess.run(
        command(impl) + ["resume", "--state-root", f"{root}/state", run_id],
        env=env_for(None, ""), cwd=root, capture_output=True, text=True,
    )
    with open(os.path.join(root, "software", "demo", "1", "PAYLOAD")) as handle:
        payload = handle.read()
    with open(os.path.join(root, "modules", "all", "demo", "1.lua")) as handle:
        module = handle.read().replace(root, "W")
    journal_path = os.path.join(root, "state", "runs", run_id, "journal.json")
    state = None
    if os.path.exists(journal_path):
        with open(journal_path) as handle:
            journal = json.load(handle)
        state = (journal["state"], [mod.get("status") for mod in journal.get("modules_plan") or []])
    hidden = sorted(name for name in os.listdir(os.path.join(root, "software", "demo")) if name.startswith("."))
    return {"resume_rc": resumed.returncode, "payload": payload, "module": module, "state": state, "hidden": hidden}


def compare(label, results):
    if results["py"] != results["zig"]:
        print(f"DIFF {label}")
        for impl in ("py", "zig"):
            print(f"     {impl}: {results[impl]}")
        return 1
    final = results["py"]
    if label.startswith("run"):
        # Published in full, or untouched when no journal was written.
        ok = (final["state"] == ("complete", ["published"]) and final["payload"] == "from-eb\n") or (
            final["state"] is None and final["payload"] == "old\n"
        )
    else:
        ok = final["state"] == ("rolled_back", ["restored"]) and final["payload"] == "old\n"
    if not ok:
        print(f"BAD  {label}: {final['state']} payload {final['payload'].strip()}")
        return 1
    print(f"same {label}: {final['state']} payload {final['payload'].strip()}")
    return 0


def main():
    if not os.environ.get("TOOL_BIN"):
        print("set TOOL_BIN", file=sys.stderr)
        return 2
    failures = 0
    steps = steps_reached()
    for step in steps:
        results = {}
        for impl in ("py", "zig"):
            root = tempfile.mkdtemp(prefix=f"crash-{impl}-")
            try:
                fixture(root)
                how = kill_at(impl, run_args(root, "r1"), step, root)
                results[impl] = {"kill": how, **outcome(impl, root, "r1")}
            finally:
                shutil.rmtree(root, ignore_errors=True)
        failures += compare(f"run killed at {step}", results)
    # Rollback of a complete run, killed at each step it reaches.
    rollback_steps = [s for s in steps_rollback()]
    for step in rollback_steps:
        results = {}
        for impl in ("py", "zig"):
            root = tempfile.mkdtemp(prefix=f"crash-rb-{impl}-")
            try:
                fixture(root)
                done = subprocess.run(command(impl) + run_args(root, "r1"), env=env_for(None, ""), cwd=root, capture_output=True)
                assert done.returncode == 0, done.stderr
                how = kill_at(impl, ["rollback", "--state-root", f"{root}/state", "r1"], step, root)
                again = subprocess.run(command(impl) + ["rollback", "--state-root", f"{root}/state", "r1"], env=env_for(None, ""), cwd=root, capture_output=True, text=True)
                info = outcome(impl, root, "r1")
                info.pop("resume_rc")
                results[impl] = {"kill": how, "rollback_rc": again.returncode, **info}
            finally:
                shutil.rmtree(root, ignore_errors=True)
        failures += compare(f"rollback killed at {step}", results)
    total = len(steps) + len(rollback_steps)
    print(f"{total - failures} of {total} cases match")
    return 1 if failures else 0


def steps_rollback():
    """Every fault step one Python rollback reaches."""
    from eb_staged_rebuild.engine import rollback

    root = tempfile.mkdtemp(prefix="crash-rbsteps-")
    try:
        fixture(root)
        spec = Spec(
            software=f"{root}/software", modules=f"{root}/modules", state_root=f"{root}/state",
            eb_args=["demo-1.eb"], backend="installpath", eb_executable=f"{root}/eb",
            module_rels=["demo/1"], run_id="r1",
        )
        assert run(spec).returncode == 0
        faults = FaultInjector()
        assert rollback(f"{root}/state", "r1", faults=faults).returncode == 0
        return list(dict.fromkeys(faults.reached))
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

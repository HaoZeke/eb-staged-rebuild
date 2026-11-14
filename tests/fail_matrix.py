"""Fail a run, and a rollback, at every step they reach; compare the outcome.

Ports the in-process failure tests of tests/test_engine.py
(test_every_recorded_failure_restores_old_tree_then_resumes,
test_failure_on_second_module_restores_both_modules,
test_every_rollback_failure_restores_published_tree) to the command
line. EB_STAGED_REBUILD_FAIL_AT=STEP makes the step fail with an I/O
error: the Python tool's FaultInjector raises OSError(EIO) there, and the
-Dtest-ops artifact named by TOOL_BIN_TEST_OPS fails the same step. Each
implementation then takes its own error path. A failed run must leave
every live tree and module file as it was, or have published all of
them; a resume must then publish. A failed rollback must leave the run
published or rolled back; a second rollback must roll it back. Both
implementations must leave the same trees, module files and journal
states.

    TOOL_BIN_TEST_OPS=zig-out/bin/eb-staged-rebuild-test-ops python3 tests/fail_matrix.py
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import crash_matrix as cm  # noqa: E402


def call(impl, fixture, root, args, fail_at=None):
    env = cm.env_for(fixture, root, None, "")
    if fail_at:
        env["EB_STAGED_REBUILD_FAIL_AT"] = fail_at
    return subprocess.run(cm.command(impl) + args, env=env, cwd=root, capture_output=True, text=True).returncode


def failed_run(fixture, step):
    def body(impl, root):
        fixture.build(root)
        rc = call(impl, fixture, root, fixture.run_args(root, "r1"), fail_at=step)
        after = cm.outcome(fixture, root, "r1")
        resumed = None
        if after["state"] is not None and after["state"][0] != "complete":
            resumed = call(impl, fixture, root, ["resume", "--state-root", f"{root}/state", "r1"])
        return {"failed_rc": rc, "after": after, "resume_rc": resumed, "final": cm.outcome(fixture, root, "r1")}
    return body


def failed_rollback(fixture, step):
    def body(impl, root):
        fixture.build(root)
        assert call(impl, fixture, root, fixture.run_args(root, "r1")) == 0
        rc = call(impl, fixture, root, ["rollback", "--state-root", f"{root}/state", "r1"], fail_at=step)
        after = cm.outcome(fixture, root, "r1")
        again = call(impl, fixture, root, ["rollback", "--state-root", f"{root}/state", "r1"])
        return {"failed_rc": rc, "after": after, "again_rc": again, "final": cm.outcome(fixture, root, "r1")}
    return body


def untouched_or_published(fixture, info):
    old = info["payloads"] == ["old\n"] * len(fixture.rels) and all("EBROOT" in m and "conflict" not in m for m in info["modules"])
    new = info["payloads"] == ["from-eb\n"] * len(fixture.rels) and all("conflict" in m for m in info["modules"])
    return old or new


def check(label, fixture, results, kind):
    py, zig = results["py"], results["zig"]
    # Exit codes are compared apart: an OSError the Python tool does not
    # catch ends in a traceback (1) where the binary refuses (2).
    same = {k: v for k, v in py.items() if not k.endswith("_rc")} == {k: v for k, v in zig.items() if not k.endswith("_rc")}
    published = ("complete", ["published"] * len(fixture.rels))
    rolled = ("rolled_back", ["restored"] * len(fixture.rels))
    if kind == "run":
        ok = untouched_or_published(fixture, py["after"]) and py["final"]["state"] in (published, None)
        ok = ok and (py["final"]["state"] is None or set(py["final"]["payloads"]) == {"from-eb\n"})
    else:
        ok = untouched_or_published(fixture, py["after"]) and py["final"]["state"] == rolled
        ok = ok and set(py["final"]["payloads"]) == {"old\n"}
    codes = (py["failed_rc"], zig["failed_rc"])
    if same and ok:
        print(f"same {label}: exit {codes[0]}/{codes[1]}, then {py['final']['state']}")
        return 0
    print(f"{'DIFF' if not same else 'BAD '} {label}")
    for impl in ("py", "zig"):
        print(f"     {impl}: {results[impl]}")
    return 1


def main():
    if not os.environ.get("TOOL_BIN_TEST_OPS"):
        print("set TOOL_BIN_TEST_OPS", file=sys.stderr)
        return 2
    os.environ["TOOL_BIN"] = os.environ["TOOL_BIN_TEST_OPS"]
    failures = total = 0
    for fixture in (cm.ONE, cm.TWO):
        for step in cm.steps_reached(fixture):
            failures += check(f"{fixture.name}: run fails at {step}", fixture, cm.each_impl("fail", failed_run(fixture, step)), "run")
            total += 1
        for step in cm.steps_rollback(fixture):
            failures += check(f"{fixture.name}: rollback fails at {step}", fixture, cm.each_impl("fail-rb", failed_rollback(fixture, step)), "rollback")
            total += 1
    print(f"{total - failures} of {total} cases match")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

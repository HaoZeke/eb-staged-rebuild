#!/usr/bin/env python3
"""Run ledger-maintenance from the Python tool and from TOOL_BIN on the same
inputs and fail on any difference in exit code, stdout, stderr or the files
they leave.

Each case builds one fixture (keys, ledger, head, manifest), copies it twice,
and runs the same steps on each copy: one copy with the Python tool, the
other with the binary. Paths are normalized to the copy's root before
comparing. A step can stop the program at a named fault point
(EB_STAGED_REBUILD_PAUSE_AT) and kill it there, as a crash would.

Needs EB_STACK_BIN (the eb-stack the ledger tests use), TOOL_BIN and openssl:

    EB_STACK_BIN=... TOOL_BIN=zig-out/bin/eb-staged-rebuild python3 tests/differential_ledger.py
"""

import datetime
import re
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time

EB = os.environ["EB_STACK_BIN"]
TOOL = os.environ["TOOL_BIN"]
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TODAY = datetime.datetime.now(datetime.timezone.utc).date()


def day(offset):
    return (TODAY + datetime.timedelta(days=offset)).isoformat()


def run(argv, **kw):
    return subprocess.run(argv, capture_output=True, text=True, timeout=120, **kw)


def eb(*argv):
    done = run([EB, "ledger", *argv])
    if done.returncode != 0:
        raise SystemExit(f"eb-stack ledger {argv[0]} failed: {done.stderr}")
    return done


class Fixture:
    def __init__(self, root, fresh=True):
        self.root = root
        self.key = self.p("keys", "ledger.pem")
        self.pub = self.p("keys", "ledger.pub")
        self.log = self.p("ledger", "stack.jsonl")
        self.head = self.p("heads", "head.json")
        self.anchors = self.p("anchors")
        self.out = self.p("out")
        self.count = 0
        if fresh:
            for name in ("keys", "ledger", "heads", "anchors", "work"):
                os.makedirs(self.p(name), exist_ok=True)
            self.genkey(self.key, self.pub)

    def p(self, *parts):
        return os.path.join(self.root, *parts)

    def genkey(self, key, pub):
        run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", key], check=True)
        run(["openssl", "pkey", "-in", key, "-pubout", "-out", pub], check=True)
        os.chmod(key, 0o400)

    def append(self, module, action, log=None, head=None, **extra):
        self.count += 1
        record = {"ts": f"2026-11-03T10:{self.count // 60:02d}:{self.count % 60:02d}Z", "actor": "site-admin",
                  "stack": "2026.06", "target": "x86_64/generic", "module": module, "location": f"/sw/{module}",
                  "action": action, "build": {"event_id": f"diff/{self.count}"}}
        record.update(extra)
        path = self.p("work", f"r{self.count}.json")
        with open(path, "w") as handle:
            json.dump(record, handle)
        eb("append", path, "--log", log or self.log, "--key", self.key, "--head-out", head or self.head)
        with open(log or self.log) as handle:
            return json.loads(handle.readlines()[-1])

    def sign(self, out=None, ts=None, key=None):
        out = out or self.head
        eb("head", "--log", self.log, "--key", key or self.key, "--out", out, "--ts", ts or f"{day(0)}T00:00:01Z")
        return out

    def manifest(self, name="manifest.json", **overrides):
        manifest = {
            "schema": "eb-staged-rebuild/observed-backfill/1", "manifest_id": "audit-1",
            "observed_at": "2026-01-02T03:04:05Z", "observer": "audit/jdoe", "stack": "2026.06",
            "target": "x86_64/generic",
            "observations": [
                {"module": "legacy/1.0", "location": "/sw/legacy/1.0",
                 "modulefile": {"path": "/modules/all/legacy/1.0.lua", "sha256": "ab" * 32},
                 "provenance": [{"ref": "/archive/legacy-1.0.log", "kind": "easybuild-log", "sha256": "cd" * 32}]},
                {"module": "évil|pkg/2", "location": "/sw/evil/2",
                 "provenance": [{"ref": "ticket <b>1234</b> é"}], "note": "found *during* audit"},
                {"module": "old/3", "location": "/sw/old/3", "provenance": [{"ref": "inventory.csv row 9"}]},
            ],
        }
        manifest.update(overrides)
        path = self.p("work", name)
        data = (json.dumps(manifest, indent=1) + "\n").encode()
        with open(path, "wb") as handle:
            handle.write(data)
        return path, hashlib.sha256(data).hexdigest()

    def wrapper(self, name, body):
        """An eb-stack that runs the real binary and then `body` on its result."""
        path = self.p("work", name)
        with open(path, "w") as handle:
            handle.write(textwrap.dedent(f"""\
                #!{sys.executable}
                import subprocess, sys
                done = subprocess.run([{EB!r}, *sys.argv[1:]], capture_output=True, text=True)
                argv, out, err, code = sys.argv[1:], done.stdout, done.stderr, done.returncode
            """) + textwrap.dedent(body) + "sys.stdout.write(out)\nsys.stderr.write(err)\nsys.exit(code)\n")
        os.chmod(path, 0o755)
        return path

    def history(self):
        """Every kind of event the projection interprets, and hostile text."""
        self.append("demo/1", "install", stage="started")
        self.append("demo/1", "install", stage="finished")
        self.append("demo/1", "rebuild-inplace", stage="started", location="/sw/demo/1-gen2")
        rebuilt = self.append("demo/1", "rebuild-inplace", stage="finished", location="/sw/demo/1-gen2")
        self.append("demo/1", "correction", stage="started", corrects=rebuilt["id"], reason="rolled back")
        rollback = self.append("demo/1", "correction", stage="finished", corrects=rebuilt["id"], reason="rolled back")
        self.append("demo/1", "access", reason={"group": "hpc", "change": "granted"})
        self.append("tool/2", "install")
        retired = self.append("tool/2", "retire", reason="published on CVMFS")
        self.append("tool/2", "correction", stage="aborted", corrects=retired["id"])
        self.append("zlib/1.3", "finding", reason="CVE-2026-0001 reported")
        self.append("demo/1", "correction", corrects=rollback["id"], reason="the rollback was itself wrong")
        hostile = "evil|pkg<script>alert(1)<\\script>`x`*[link](javascript:e)\\|"
        self.append(f"{hostile}/1", "install", actor="a\n# injected heading\n| x | y |", reason="‮txt.exe & <b>")
        self.append("../../../etc/1", "install", supersedes="0" * 64)
        self.append("café/1", "access", reason="ok")


def scrub(data):
    """Bytes or text with what legitimately differs between two runs replaced:
    today's signing times, temporary and staging directory names, lock pids."""
    text = data.decode("utf-8", "surrogateescape") if isinstance(data, bytes) else data
    text = re.sub(re.escape(TODAY.isoformat()) + r"T\d\d:\d\d:\d\dZ", "<NOW>", text)
    text = re.sub(r"\.anchor-staging-[a-z0-9_]{8}", ".anchor-staging-<X>", text)
    text = re.sub(r"/eb-ledger-(verify|backfill|anchors)-[a-z0-9_]{8}", r"/eb-ledger-\1-<X>", text)
    return text.encode("utf-8", "surrogateescape") if isinstance(data, bytes) else text


def tree(root):
    files = {}
    if not os.path.isdir(root):
        return files
    for directory, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(directory, name)
            with open(path, "rb") as handle:
                data = handle.read()
            rel = os.path.relpath(path, root)
            data = b"<pid>" if os.path.basename(rel) == ".lock" else scrub(data)
            files[rel] = (data, os.lstat(path).st_mode & 0o777, os.lstat(path).st_nlink)
    return files


def anchors_state(f):
    """Anchor names, modes and links; staging leftovers by prefix only."""
    state = []
    for name in sorted(os.listdir(f.anchors)):
        info = os.lstat(os.path.join(f.anchors, name))
        state.append(("staging" if name.startswith(".anchor-staging-") else name, info.st_mode & 0o777, info.st_nlink))
    return state


def log_state(f):
    files = sorted(os.listdir(f.p("ledger")))
    with open(f.log, "rb") as handle:
        return [".quarantine." in n for n in files], handle.read()


failures = []
cases = 0


def command(side, sub, args, env=None):
    if side == "py":
        return [sys.executable, "-m", "eb_staged_rebuild.ledger_maintenance", sub, *args], REPO
    return [TOOL, "ledger-maintenance", sub, *args], None


def execute(side, sub, args, kill_at=None):
    argv, cwd = command(side, sub, args)
    if kill_at is None:
        return run(argv, cwd=cwd)
    ready = tempfile.mktemp(prefix="ledger-diff-ready-")
    env = dict(os.environ, EB_STAGED_REBUILD_PAUSE_AT=kill_at, EB_STAGED_REBUILD_PAUSE_READY=ready,
               EB_STAGED_REBUILD_PAUSE_SECONDS="60")
    child = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 60
    while not os.path.exists(ready) and child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    reached = os.path.exists(ready)
    child.send_signal(signal.SIGKILL)
    child.communicate()
    if os.path.exists(ready):
        os.unlink(ready)
    return subprocess.CompletedProcess(argv, "killed" if reached else "not reached", "", "")


def case(name, setup, steps, state=None):
    """setup(fx) builds the fixture; steps are (sub, args(f), kill_at) run on both copies."""
    global cases
    cases += 1
    base = tempfile.mkdtemp(prefix="ledger-diff-")
    try:
        seed = Fixture(os.path.join(base, "seed"))
        setup(seed)
        roots = {}
        for side in ("py", "zig"):
            roots[side] = os.path.join(base, side)
            shutil.copytree(seed.root, roots[side], symlinks=True)
        fx = {side: Fixture(root, fresh=False) for side, root in roots.items()}
        for number, (sub, args, kill_at) in enumerate(steps):
            got = {}
            for side, f in fx.items():
                done = execute(side, sub, args(f), kill_at)
                norm = lambda text, root=roots[side]: scrub((text or "").replace(root, "<ROOT>"))
                got[side] = (done.returncode, norm(done.stdout), norm(done.stderr))
            if got["py"] != got["zig"]:
                failures.append(f"{name} step {number}")
                print(f"FAIL {name} step {number} ({sub})\n  python: {got['py']!r}\n  zig:    {got['zig']!r}")
                return
        if state is not None:
            a, b = state(fx["py"]), state(fx["zig"])
            if a != b:
                failures.append(f"{name} state")
                print(f"FAIL {name}: state differs\n  python: {str(a)[:600]}\n  zig:    {str(b)[:600]}")
                return
        print(f"ok   {name} ({', '.join(str(got['py'][0]) for _ in [0])})")
    finally:
        shutil.rmtree(base, ignore_errors=True)


def common(f, eb_stack=None):
    return ["--eb-stack", eb_stack or EB, "--log", f.log, "--public-key", f.pub]


def backfill_args(f, manifest="manifest.json", digest=None, **over):
    path = f.p("work", manifest)
    with open(path, "rb") as handle:
        real = hashlib.sha256(handle.read()).hexdigest()
    args = dict(log=f.log, key=f.key, head=f.head)
    args.update(over)
    return ["--eb-stack", EB, "--log", args["log"], "--public-key", f.pub, "--key", args["key"],
            "--head", args["head"], "--manifest", path, "--manifest-sha256", digest or real]


def anchor_args(f, key=None, anchors=None, eb_stack=None):
    return [*common(f, eb_stack), "--key", key or f.key, "--anchors", anchors or f.anchors]


def verify_args(f):
    return [*common(f), "--anchors", f.anchors]


def project_args(f, head=None, out=None, eb_stack=None):
    return [*common(f, eb_stack), "--head", head or f.head, "--out", out or f.out]


def main():
    nothing = lambda fx: None

    # Backfill
    def with_manifest(fx):
        fx.manifest()
    case("backfill: findings at the observation time, then a projection", with_manifest,
         [("backfill", backfill_args, None), ("backfill", backfill_args, None), ("project", project_args, None)],
         state=lambda f: (log_state(f), tree(f.out)))
    second = "eb-staged-rebuild/backfill/audit-1/évil|pkg/2@" + hashlib.sha256(b"/sw/evil/2").hexdigest()[:16]
    case("backfill: an interrupted backfill resumes without duplicates", with_manifest,
         [("backfill", backfill_args, f"backfill.append:{second}"), ("backfill", backfill_args, None)],
         state=log_state)

    def torn(fx):
        fx.append("demo/1", "install")
        with open(fx.log, "ab") as handle:
            handle.write(b'{"action":"inst')
        fx.manifest()
    case("backfill: a torn tail is quarantined first", torn, [("backfill", backfill_args, None)],
         state=lambda f: log_state(f)[0])

    def changed(fx):
        fx.manifest()
        path, digest = fx.manifest(name="changed.json", observations=[
            {"module": "legacy/1.0", "location": "/sw/legacy/1.0", "provenance": [{"ref": "a different source"}]},
            {"module": "new/9", "location": "/sw/new/9", "provenance": [{"ref": "x"}]}])
    case("backfill: changed manifest content is refused", changed,
         [("backfill", backfill_args, None), ("backfill", lambda f: backfill_args(f, "changed.json"), None),
          ("backfill", lambda f: backfill_args(f, "changed.json", digest="0" * 64), None)],
         state=log_state)

    def other_content(fx):
        fx.manifest()
        event = "eb-staged-rebuild/backfill/audit-1/old/3@" + hashlib.sha256(b"/sw/old/3").hexdigest()[:16]
        fx.append("old/3", "finding", ts="2026-01-02T03:04:05Z", build={"event_id": event})
    case("backfill: an event recorded with other content is refused", other_content,
         [("backfill", backfill_args, None)], state=log_state)

    guessed = [
        ({"observed_at": "2099-01-01T00:00:00Z"}, "future"),
        ({"observed_at": "2026-11-02"}, "date only"),
        ({"observed_at": "2026-11-02T09:30:00+01:00"}, "offset"),
        ({"observed_at": "2026-02-30T00:00:00Z"}, "not a time"),
        ({"observations": [{"module": "a/1", "location": "/sw/a/1", "installed_at": "x", "provenance": [{"ref": "r"}]}]}, "unknown field"),
        ({"observations": [{"module": "a/1", "location": "/sw/a/1", "provenance": []}]}, "no provenance"),
        ({"observations": [{"module": "a/1", "location": "sw/a/1", "provenance": [{"ref": "r"}]}]}, "relative"),
        ({"observations": [{"module": "a/1", "location": "/sw/a/1", "provenance": [{"ref": "r", "date": "x"}]}]}, "ref field"),
        ({"observations": [{"module": "a/1", "location": "/sw/a/1", "provenance": [{"ref": "r"}]}] * 2}, "repeats"),
        ({"observations": [{"module": " a/1", "location": "/sw/a/1", "provenance": [{"ref": "r"}]}]}, "edge space"),
        ({"observations": [{"module": "a‮/1", "location": "/sw/a/1", "provenance": [{"ref": "r"}]}]}, "bidi"),
        ({"stage": "finished"}, "top-level field"),
        ({"schema": "other"}, "schema"),
        ({"manifest_id": "-x"}, "manifest id"),
        ({"observations": []}, "empty"),
    ]
    for overrides, label in guessed:
        case(f"backfill: refused input, {label}", lambda fx, o=overrides: fx.manifest(**o),
             [("backfill", backfill_args, None)], state=lambda f: os.path.exists(f.log))
    case("backfill: an unpinned digest is refused", with_manifest,
         [("backfill", lambda f: backfill_args(f, digest="0" * 64), None),
          ("backfill", lambda f: backfill_args(f, digest="XYZ"), None)], state=lambda f: os.path.exists(f.log))

    def aliases(fx):
        fx.manifest()
        os.symlink(fx.p("ledger"), fx.p("alias"))
        shutil.copy(fx.key, fx.p("ledger", "key.pem"))
    case("backfill: path aliases and a key beside the log are refused", aliases,
         [("backfill", lambda f: backfill_args(f, log=f.p("alias", "stack.jsonl")), None),
          ("backfill", lambda f: backfill_args(f, log=f.p("ledger", "..", "ledger", "stack.jsonl")), None),
          ("backfill", lambda f: backfill_args(f, key=f.p("ledger", "key.pem")), None),
          ("backfill", lambda f: backfill_args(f, head=f.key), None)],
         state=lambda f: os.path.exists(f.log))

    # Anchors
    one = lambda fx: fx.append("demo/1", "install")
    case("anchor: the first anchor is read-only, single-link and verifies", one,
         [("anchor", anchor_args, None), ("anchor", anchor_args, None), ("verify-anchors", verify_args, None)],
         state=anchors_state)
    case("anchor: an empty log", lambda fx: open(fx.log, "w").close(),
         [("anchor", anchor_args, None), ("verify-anchors", verify_args, None)], state=anchors_state)

    def chain(fx):
        fx.append("demo/1", "install")
        fx.sign(fx.p("anchors", f"{day(-2)}.head.json"), ts=f"{day(-2)}T12:00:00Z")
        fx.append("demo/2", "install")
        fx.sign(fx.p("anchors", f"{day(-1)}.head.json"), ts=f"{day(-1)}T12:00:00Z")
        fx.append("demo/3", "install")
        for name in os.listdir(fx.anchors):
            os.chmod(fx.p("anchors", name), 0o444)
    case("anchor: daily anchors form a verified chain", chain,
         [("anchor", anchor_args, None), ("verify-anchors", verify_args, None)], state=anchors_state)

    def rewritten(fx):
        fx.append("demo/1", "install")
        fx.append("demo/2", "install")
        fx.sign(fx.p("anchors", f"{day(-1)}.head.json"), ts=f"{day(-1)}T12:00:00Z")
        other = fx.p("work", "rewritten.jsonl")
        fx.append("demo/1", "install", log=other, head=fx.p("work", "other-head.json"), location="/sw/evil")
        for number in range(3):
            fx.append(f"demo/{number + 2}", "install", log=other, head=fx.p("work", "other-head.json"))
        shutil.copy(other, fx.log)
    case("anchor: a rewritten log is refused", rewritten,
         [("anchor", anchor_args, None), ("verify-anchors", verify_args, None)], state=anchors_state)

    def truncated(fx):
        for number in range(3):
            fx.append(f"demo/{number}", "install")
        fx.sign(fx.p("anchors", f"{day(-1)}.head.json"), ts=f"{day(-1)}T12:00:00Z")
        with open(fx.log, "rb") as handle:
            data = handle.read()
        with open(fx.log, "wb") as handle:
            handle.write(data[: data.index(b"\n") + 1])
    case("anchor: a truncated log is refused", truncated, [("anchor", anchor_args, None)], state=anchors_state)

    def tampered(kind):
        def setup(fx):
            fx.append("demo/1", "install")
            path = fx.sign(fx.p("anchors", f"{day(-1)}.head.json"), ts=f"{day(-1)}T12:00:00Z")
            fx.append("demo/2", "install")
            with open(path, "rb") as handle:
                good = handle.read()
            head = json.loads(good)
            data = {
                "signature": (json.dumps(dict(head, signature=("0" if head["signature"][0] != "0" else "1") + head["signature"][1:]),
                                         separators=(",", ":")) + "\n").encode(),
                "root": (json.dumps(dict(head, root="0" * 64), separators=(",", ":")) + "\n").encode(),
                "truncated": good[:40],
                "extra field": good[:-2] + b',"x":1}\n',
            }[kind]
            with open(path, "wb") as handle:
                handle.write(data)
        return setup
    for kind in ("signature", "root", "truncated", "extra field"):
        case(f"anchor: a tampered anchor ({kind}) is refused and kept", tampered(kind),
             [("anchor", anchor_args, None)], state=lambda f: (anchors_state(f), tree(f.anchors)))

    def later_day(fx):
        fx.append("demo/1", "install")
        fx.sign(fx.p("anchors", f"{day(1)}.head.json"), ts=f"{day(1)}T12:00:00Z")
    case("anchor: an anchor from a later day is refused", later_day, [("anchor", anchor_args, None)], state=anchors_state)

    def other_key(fx):
        fx.append("demo/1", "install")
        fx.sign(fx.p("anchors", f"{day(-1)}.head.json"), ts=f"{day(-1)}T12:00:00Z")
        fx.genkey(fx.p("keys", "other.pem"), fx.p("keys", "other.pub"))
    case("anchor: a head signed by another key is refused", other_key,
         [("anchor", lambda f: anchor_args(f, key=f.p("keys", "other.pem")), None)], state=anchors_state)

    for step in ("anchor.sign", "anchor.publish"):
        case(f"anchor: a crash at {step} leaves no anchor", one,
             [("anchor", anchor_args, step), ("verify-anchors", verify_args, None), ("anchor", anchor_args, None)],
             state=anchors_state)
    case("anchor: a crash after the link is recovered", one,
         [("anchor", anchor_args, "anchor.published"), ("verify-anchors", verify_args, None),
          ("anchor", anchor_args, None), ("verify-anchors", verify_args, None)], state=anchors_state)

    def renamed(fx):
        fx.append("demo/1", "install")
        fx.sign(fx.p("anchors", f"{day(-2)}.head.json"), ts=f"{day(-1)}T12:00:00Z")
    case("anchor: an anchor whose name is not its day is refused", renamed,
         [("anchor", anchor_args, None)], state=anchors_state)

    def linked(fx):
        fx.append("demo/1", "install")
        path = fx.sign(fx.p("anchors", f"{day(-1)}.head.json"), ts=f"{day(-1)}T12:00:00Z")
        os.link(path, fx.p("work", "copy.json"))
    case("anchor: an anchor with a second link is refused", linked, [("anchor", anchor_args, None)], state=anchors_state)

    receipts = {
        "index checkpoint": 'if argv[:2] == ["ledger", "head"]:\n    out += "index checkpoint eb-stack-ledger/demo/2026/index size 1 root " + "0" * 64 + " to /x\\n"\n',
        "noisy head": 'if argv[:2] == ["ledger", "head"]:\n    out += "extra\\n"\n',
        "lying verify": 'if argv[:2] == ["ledger", "verify"]:\n    out = out.replace("size 1", "size 2")\n',
        "failing verify": 'if argv[:2] == ["ledger", "verify"]:\n    code, out = 1, ""\n',
        "failing head": 'if argv[:2] == ["ledger", "head"]:\n    code, err = 3, "no key\\n"\n',
    }
    for label, body in receipts.items():
        def setup(fx, body=body):
            fx.append("demo/1", "install")
            fx.wrapper("eb-wrapped", body)
        case(f"anchor: receipt, {label}", setup,
             [("anchor", lambda f: anchor_args(f, eb_stack=f.p("work", "eb-wrapped")), None)], state=anchors_state)

    def anchor_paths(fx):
        fx.append("demo/1", "install")
        shutil.copy(fx.key, fx.p("anchors", "key.pem"))
        os.symlink(fx.anchors, fx.p("anchors-alias"))
        os.link(fx.log, fx.p("work", "log-alias"))
    case("anchor: paths are checked", anchor_paths,
         [("anchor", lambda f: anchor_args(f, anchors=f.p("ledger")), None),
          ("anchor", lambda f: anchor_args(f, key=f.p("anchors", "key.pem")), None),
          ("anchor", lambda f: anchor_args(f, anchors=f.p("anchors-alias")), None),
          ("anchor", anchor_args, None)], state=anchors_state)

    # Projection
    def history(fx):
        fx.history()
        fx.manifest()
    case("project: history, corrections, findings and hostile text", history,
         [("backfill", backfill_args, None), ("project", project_args, None),
          ("project", lambda f: project_args(f, out=f.p("two")), None), ("project", project_args, None)],
         state=lambda f: (tree(f.out), tree(f.p("two"))))
    case("project: an interrupted projection is completed by a rerun", history,
         [("backfill", backfill_args, None), ("project", project_args, "projection.packages/old.md.rename"),
          ("project", project_args, None)], state=lambda f: tree(f.out))
    case("project: a crash before the index is replaced keeps the old index", history,
         [("backfill", backfill_args, None), ("project", project_args, None),
          ("project", project_args, "projection.index.json.fsync")], state=lambda f: tree(f.out))

    def tampered_log(kind):
        def setup(fx):
            for number in range(3):
                fx.append(f"demo/{number}", "install")
            with open(fx.log, "rb") as handle:
                good = handle.read()
            if kind == "edited":
                data = good.replace(b"/sw/demo/1", b"/sw/demo/X", 1)
            elif kind == "partial":
                data = good + b'{"partial'
            else:
                other = fx.p("work", "rewritten.jsonl")
                for number in range(3):
                    fx.append(f"demo/{number}", "install", log=other, head=fx.p("work", "other-head.json"),
                              location=f"/sw/rewritten/{number}")
                with open(other, "rb") as handle:
                    data = handle.read()
            with open(fx.log, "wb") as handle:
                handle.write(data)
        return setup
    for kind in ("edited", "rewritten", "partial"):
        case(f"project: a {kind} log is refused and nothing is written", tampered_log(kind),
             [("project", project_args, None)], state=lambda f: os.path.exists(f.out))

    def heads(fx):
        for number in range(2):
            fx.append(f"demo/{number}", "install")
        with open(fx.head) as handle:
            head = json.loads(handle.read())
        cases_ = {
            "root": json.dumps(dict(head, root="1" * 64), separators=(",", ":")) + "\n",
            "size": json.dumps(dict(head, size=1), separators=(",", ":")) + "\n",
            "ts": json.dumps(dict(head, ts="2030-01-01T00:00:00Z"), separators=(",", ":")) + "\n",
            "domain": json.dumps(dict(head, domain="other-log"), separators=(",", ":")) + "\n",
            "spaced": json.dumps(head) + "\n",
            "notjson": "{",
            "dupkey": json.dumps(head, separators=(",", ":"))[:-1] + ',"size":2}\n',
        }
        for name, text in cases_.items():
            with open(fx.p("heads", f"{name}.json"), "w") as handle:
                handle.write(text)
        fx.genkey(fx.p("keys", "other.pem"), fx.p("keys", "other.pub"))
        eb("head", "--log", fx.log, "--key", fx.p("keys", "other.pem"), "--out", fx.p("heads", "foreign.json"))
    case("project: tampered and foreign heads are refused", heads,
         [("project", lambda f, n=n: project_args(f, head=f.p("heads", f"{n}.json")), None)
          for n in ("root", "size", "ts", "domain", "spaced", "notjson", "dupkey", "foreign")]
         + [("project", project_args, None)], state=lambda f: tree(f.out))

    def stale(fx):
        fx.append("demo/1", "install")
        shutil.copy(fx.head, fx.p("heads", "stale.json"))
        fx.append("demo/2", "install")
    case("project: a stale head is refused", stale,
         [("project", lambda f: project_args(f, head=f.p("heads", "stale.json")), None), ("project", project_args, None)],
         state=lambda f: tree(f.out))

    def outdir(fx):
        fx.append("demo/1", "install")
        os.makedirs(fx.out)
        with open(fx.p("out", "notes.txt"), "w") as handle:
            handle.write("mine")
        os.symlink(fx.p("work"), fx.p("out-alias"))
        os.makedirs(fx.p("fake"))
        with open(fx.p("fake", "index.json"), "w") as handle:
            handle.write('{"schema":"something else"}')
    case("project: the output directory is checked", outdir,
         [("project", project_args, None), ("project", lambda f: project_args(f, out=f.p("ledger")), None),
          ("project", lambda f: project_args(f, out=f.p("out-alias")), None),
          ("project", lambda f: project_args(f, out=f.p("fake")), None)], state=lambda f: tree(f.out))

    for label, body in {"silent": 'if argv[:2] == ["ledger", "verify"]:\n    out = ""\n',
                        "doubled": 'if argv[:2] == ["ledger", "verify"]:\n    out = out + out\n',
                        "tiles fail": 'if argv[:2] == ["ledger", "tiles"]:\n    code, err = 2, "disk\\n"\n'}.items():
        def setup(fx, body=body):
            fx.append("demo/1", "install")
            fx.wrapper("eb-wrapped", body)
        case(f"project: verify receipt, {label}", setup,
             [("project", lambda f: project_args(f, eb_stack=f.p("work", "eb-wrapped")), None)],
             state=lambda f: os.path.exists(f.out))

    def hanging(fx):
        fx.append("demo/1", "install")
        fx.wrapper("eb-wrapped", 'if argv[:2] == ["ledger", "verify"]:\n    import time; time.sleep(30)\n')
    case("project: a hanging eb-stack times out with exit 75", hanging,
         [("project", lambda f: [*project_args(f, eb_stack=f.p("work", "eb-wrapped")), "--timeout", "1"], None)])

    # Command line
    case("command line: bad arguments", one,
         [("project", lambda f: [*project_args(f), "--timeout", "0"], None),
          ("project", lambda f: [*project_args(f), "--timeout", "-3"], None)])

    if failures:
        print(f"{len(failures)} of {cases} case(s) differ")
        return 1
    print(f"all {cases} cases match")
    return 0


if __name__ == "__main__":
    sys.exit(main())

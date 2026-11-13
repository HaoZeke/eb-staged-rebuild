#!/usr/bin/env python3
"""Run ledger-maintenance from the Python tool and from TOOL_BIN on the same
inputs and fail on any difference in exit code, stdout, stderr or the files
they write.

Needs EB_STACK_BIN (the eb-stack the ledger tests use), TOOL_BIN and openssl.
Each case runs in two copies of one fixture tree, so both tools see the same
bytes; paths are normalized to the copy's root before comparing.

    EB_STACK_BIN=... TOOL_BIN=zig-out/bin/eb-staged-rebuild python3 tests/differential_ledger.py
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

EB = os.environ["EB_STACK_BIN"]
TOOL = os.environ["TOOL_BIN"]
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(argv, **kw):
    return subprocess.run(argv, capture_output=True, text=True, timeout=120, **kw)


def eb(*argv):
    done = run([EB, "ledger", *argv])
    if done.returncode != 0:
        raise SystemExit(f"eb-stack ledger {argv[0]} failed: {done.stderr}")
    return done


class Fixture:
    def __init__(self, root):
        self.root = root
        for name in ("keys", "ledger", "heads", "anchors", "work"):
            os.makedirs(os.path.join(root, name), exist_ok=True)
        self.key = self.p("keys", "ledger.pem")
        self.pub = self.p("keys", "ledger.pub")
        run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", self.key], check=True)
        run(["openssl", "pkey", "-in", self.key, "-pubout", "-out", self.pub], check=True)
        os.chmod(self.key, 0o400)
        self.log = self.p("ledger", "stack.jsonl")
        self.head = self.p("heads", "head.json")
        self.count = 0

    def p(self, *parts):
        return os.path.join(self.root, *parts)

    def append(self, module, action, **extra):
        self.count += 1
        record = {"ts": f"2026-11-03T10:00:{self.count:02d}Z", "actor": "site-admin", "stack": "2026.06",
                  "target": "x86_64/generic", "module": module, "location": f"/sw/{module}",
                  "action": action, "build": {"event_id": f"diff/{self.count}"}}
        record.update(extra)
        path = self.p("work", f"r{self.count}.json")
        with open(path, "w") as handle:
            json.dump(record, handle)
        eb("append", path, "--log", self.log, "--key", self.key, "--head-out", self.head)
        with open(self.log) as handle:
            return json.loads(handle.readlines()[-1])

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
            ],
        }
        manifest.update(overrides)
        path = self.p("work", name)
        data = (json.dumps(manifest, indent=1) + "\n").encode()
        with open(path, "wb") as handle:
            handle.write(data)
        return path, hashlib.sha256(data).hexdigest()


def build_fixture(root):
    fx = Fixture(root)
    first = fx.append("demo/1", "install", reason="first install")
    fx.append("demo/1", "rebuild-inplace", stage="started")
    fx.append("demo/1", "rebuild-inplace", stage="finished")
    fx.append("demo/1", "correction", corrects=first["id"], stage="started")
    fx.append("demo/1", "correction", corrects=first["id"], stage="finished", reason={"why": "<typo>", "n": 2})
    fx.append("tools‮/3", "install", supersedes="0" * 64)
    fx.append("tools‮/3", "access")
    fx.append("other/9", "retire", moved_to="1" * 64)
    return fx


def tools(fx, sub, *argv):
    py = run([sys.executable, "-m", "eb_staged_rebuild.ledger_maintenance", sub, *argv], cwd=REPO)
    zig = run([TOOL, "ledger-maintenance", sub, *argv])
    return py, zig


def tree(root):
    files = {}
    for directory, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(directory, name)
            with open(path, "rb") as handle:
                files[os.path.relpath(path, root)] = (handle.read(), os.stat(path).st_mode & 0o777)
    return files


failures = []


def compare(case, py, zig, py_root, zig_root, py_tree=None, zig_tree=None):
    def norm(text, root):
        return text.replace(root, "<ROOT>")
    got = [(py.returncode, norm(py.stdout, py_root), norm(py.stderr, py_root)),
           (zig.returncode, norm(zig.stdout, zig_root), norm(zig.stderr, zig_root))]
    if got[0] != got[1]:
        failures.append(case)
        print(f"FAIL {case}\n  python: {got[0]!r}\n  zig:    {got[1]!r}")
        return
    if py_tree is not None and tree(py_tree) != tree(zig_tree):
        a, b = tree(py_tree), tree(zig_tree)
        diff = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
        failures.append(case)
        print(f"FAIL {case}: files differ: {diff}")
        return
    print(f"ok   {case} (exit {py.returncode})")


def main():
    base = tempfile.mkdtemp(prefix="ledger-diff-")
    try:
        seed = os.path.join(base, "seed")
        fx = build_fixture(seed)
        eb("head", "--log", fx.log, "--key", fx.key, "--out", fx.head, "--ts", "2026-11-03T12:00:00Z")
        roots = {}
        for side in ("py", "zig"):
            roots[side] = os.path.join(base, side)
            shutil.copytree(seed, roots[side], symlinks=True)
        P = {side: Fixture.__new__(Fixture) for side in roots}
        for side, root in roots.items():
            P[side].root = root
            P[side].log, P[side].head = os.path.join(root, "ledger", "stack.jsonl"), os.path.join(root, "heads", "head.json")
            P[side].key, P[side].pub = os.path.join(root, "keys", "ledger.pem"), os.path.join(root, "keys", "ledger.pub")
            P[side].count = fx.count

        def both(case, sub, make_args, tree_of=None):
            py = run([sys.executable, "-m", "eb_staged_rebuild.ledger_maintenance", sub, *make_args(P["py"])], cwd=REPO)
            zig = run([TOOL, "ledger-maintenance", sub, *make_args(P["zig"])])
            compare(case, py, zig, roots["py"], roots["zig"],
                    tree_of(P["py"]) if tree_of else None, tree_of(P["zig"]) if tree_of else None)

        common = lambda f: ["--eb-stack", EB, "--log", f.log, "--public-key", f.pub]
        both("project", "project", lambda f: [*common(f), "--head", f.head, "--out", f.p("out")], lambda f: f.p("out"))
        both("project again", "project", lambda f: [*common(f), "--head", f.head, "--out", f.p("out")], lambda f: f.p("out"))
        both("project into the ledger directory", "project", lambda f: [*common(f), "--head", f.head, "--out", f.p("ledger")])
        both("project with a missing head", "project", lambda f: [*common(f), "--head", f.p("heads", "none.json"), "--out", f.p("o2")])
        both("verify-anchors, none yet", "verify-anchors", lambda f: [*common(f), "--anchors", f.p("anchors")])
        both("timeout must be positive", "project", lambda f: [*common(f), "--head", f.head, "--out", f.p("o3"), "--timeout", "0"])
        for side in P:
            Fixture.manifest(P[side])
        digest = Fixture.manifest(P["py"])[1]
        both("backfill, wrong digest", "backfill", lambda f: [*common(f), "--key", f.key, "--head", f.head,
                                                             "--manifest", f.p("work", "manifest.json"), "--manifest-sha256", "0" * 64])
        both("backfill", "backfill", lambda f: [*common(f), "--key", f.key, "--head", f.head,
                                               "--manifest", f.p("work", "manifest.json"), "--manifest-sha256", digest])
        both("backfill again appends nothing", "backfill", lambda f: [*common(f), "--key", f.key, "--head", f.head,
                                                                      "--manifest", f.p("work", "manifest.json"), "--manifest-sha256", digest])
        both("project after backfill", "project", lambda f: [*common(f), "--head", f.head, "--out", f.p("out")], lambda f: f.p("out"))
        both("anchor", "anchor", lambda f: [*common(f), "--key", f.key, "--anchors", f.p("anchors")])
        both("anchor again", "anchor", lambda f: [*common(f), "--key", f.key, "--anchors", f.p("anchors")])
        both("verify-anchors", "verify-anchors", lambda f: [*common(f), "--anchors", f.p("anchors")])
        for side in P:
            signed = P[side].head
            P[side].head = P[side].p("work", "newer.json")
            Fixture.append(P[side], "late/1", "install")
            P[side].head = signed
        both("stale head", "project", lambda f: [*common(f), "--head", f.head, "--out", f.p("out")])
    finally:
        shutil.rmtree(base, ignore_errors=True)
    if failures:
        print(f"{len(failures)} case(s) differ")
        return 1
    print("all cases match")
    return 0


if __name__ == "__main__":
    sys.exit(main())

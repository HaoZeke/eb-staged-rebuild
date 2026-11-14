"""Ledger maintenance against the real eb-stack ledger.

Every signature and proof here comes from the eb-stack binary named by
``EB_STACK_BIN``; nothing stands in for it. Without that binary (and openssl
for keys) the real-ledger tests fail rather than pass unexercised. The input
checks at the end need neither.
"""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

from eb_staged_rebuild import ledger_maintenance as lm
from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.faults import FaultInjector, InjectedCrash
from eb_staged_rebuild.outbox import PublisherError

DAY1 = 1793707200  # 2026-11-03T12:00:00Z
DAY2 = DAY1 + 86400
DAY3 = DAY2 + 86400
OBSERVED = "2026-11-02T09:30:00Z"


def cell_separators(line):
    """Pipes in a Markdown table row that are not backslash-escaped."""
    count, escaped = 0, False
    for char in line:
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            count += 1
    return count


def tree(root):
    """Every file under ``root`` with its bytes."""
    found = {}
    for directory, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(directory, name)
            with open(path, "rb") as handle:
                found[os.path.relpath(path, root)] = handle.read()
    return found


class RealLedger(unittest.TestCase):
    def setUp(self):
        binary = os.environ.get("EB_STACK_BIN")
        if not binary or not os.path.isfile(binary) or not shutil.which("openssl"):
            self.fail("EB_STACK_BIN must name an eb-stack binary and openssl must be on PATH")
        self.eb = os.path.realpath(binary)
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(self._remove)
        for name in ("keys", "ledger", "heads", "anchors", "work"):
            os.makedirs(os.path.join(self.root, name))
        self.key = self.path("keys", "ledger.pem")
        self.pub = self.path("keys", "ledger.pub")
        self.genkey(self.key, self.pub)
        self.log = self.path("ledger", "stack.jsonl")
        self.head = self.path("heads", "head.json")
        self.anchors = self.path("anchors")
        self.out = self.path("out")
        self.count = 0

    def _remove(self):
        for directory, dirs, files in os.walk(self.root):
            for name in dirs + files:
                path = os.path.join(directory, name)
                if not os.path.islink(path):
                    os.chmod(path, 0o700)
        shutil.rmtree(self.root, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def genkey(self, key, pub):
        subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", key], check=True, capture_output=True)
        subprocess.run(["openssl", "pkey", "-in", key, "-pubout", "-out", pub], check=True, capture_output=True)
        os.chmod(key, stat.S_IRUSR)

    def cli(self, *argv, check=True):
        done = subprocess.run([self.eb, "ledger", *argv], check=False, capture_output=True, text=True, timeout=60)
        if check:
            self.assertEqual(done.returncode, 0, done.stderr)
        return done

    def append(self, module, action, log=None, head=None, **extra):
        self.count += 1
        record = {
            "ts": f"2026-11-03T10:{self.count // 60:02d}:{self.count % 60:02d}Z", "actor": "site-admin",
            "stack": "2026.06", "target": "x86_64/generic", "module": module,
            "location": f"/sw/{module}", "action": action,
            "build": {"event_id": f"test/{self.count}"},
        }
        record.update(extra)
        path = self.path("work", f"record-{self.count}.json")
        with open(path, "w") as handle:
            json.dump(record, handle)
        self.cli("append", path, "--log", log or self.log, "--key", self.key, "--head-out", head or self.head)
        return self.records(log)[-1]

    def records(self, log=None):
        with open(log or self.log) as handle:
            return [json.loads(line) for line in handle]

    def log_bytes(self):
        with open(self.log, "rb") as handle:
            return handle.read()

    def sign(self, out=None, ts="2026-11-03T12:00:00Z"):
        out = out or self.head
        self.cli("head", "--log", self.log, "--key", self.key, "--out", out, "--ts", ts)
        return out

    def verify_record(self, index, head=None):
        return self.cli("verify", "--log", self.log, "--public-key", self.pub, "--record", str(index),
                        "--head", head or self.head, check=False)

    def write_manifest(self, observations=None, name="manifest.json", **overrides):
        manifest = {
            "schema": lm.MANIFEST_SCHEMA, "manifest_id": "site-audit-1", "observed_at": OBSERVED,
            "observer": "audit/jdoe", "stack": "2026.06", "target": "x86_64/generic",
            "observations": observations or [
                {"module": "legacy/1.0", "location": "/sw/legacy/1.0",
                 "modulefile": {"path": "/modules/all/legacy/1.0.lua", "sha256": "ab" * 32},
                 "provenance": [{"ref": "/archive/easybuild/legacy-1.0.log", "kind": "easybuild-log",
                                 "sha256": "cd" * 32}]},
                {"module": "old/2", "location": "/sw/old/2",
                 "provenance": [{"ref": "ticket HPC-1234"}], "note": "found during audit"},
                {"module": "old/3", "location": "/sw/old/3", "provenance": [{"ref": "inventory.csv row 9"}]},
            ],
        }
        manifest.update(overrides)
        path = self.path("work", name)
        data = (json.dumps(manifest, indent=1) + "\n").encode()
        with open(path, "wb") as handle:
            handle.write(data)
        return path, hashlib.sha256(data).hexdigest()

    def backfill(self, manifest, digest, faults=None, eb=None):
        return lm.backfill(eb or self.eb, self.log, self.key, self.head, self.pub, manifest, digest,
                           timeout=60, faults=faults, clock=lambda: DAY1)

    def anchor(self, clock=DAY1, faults=None, eb=None, key=None, anchors=None, log=None):
        return lm.anchor(eb or self.eb, log or self.log, key or self.key, self.pub, anchors or self.anchors,
                         timeout=60, faults=faults, clock=lambda: clock)

    def project(self, out=None, head=None, faults=None, eb=None):
        return lm.project(eb or self.eb, self.log, head or self.head, self.pub, out or self.out,
                          timeout=60, faults=faults)

    def wrapper(self, body):
        """An eb-stack that runs the real binary and then ``body`` on its result."""
        path = self.path("work", "eb-stack-wrapper")
        with open(path, "w") as handle:
            handle.write(textwrap.dedent(f"""\
                #!{sys.executable}
                import subprocess, sys
                done = subprocess.run([{self.eb!r}, *sys.argv[1:]], capture_output=True, text=True)
                argv, out, err, code = sys.argv[1:], done.stdout, done.stderr, done.returncode
            """) + textwrap.dedent(body) + "sys.stdout.write(out)\nsys.stderr.write(err)\nsys.exit(code)\n")
        os.chmod(path, 0o755)
        return path


class BackfillTests(RealLedger):
    def test_observations_become_findings_at_the_observation_time(self):
        manifest, digest = self.write_manifest()
        result = self.backfill(manifest, digest)
        self.assertEqual((result["appended"], result["replayed"]), (3, 0))
        self.assertTrue(all(receipt["verified"] for receipt in result["receipts"]))
        records = self.records()
        self.assertEqual([record["module"] for record in records], ["legacy/1.0", "old/2", "old/3"])
        for record in records:
            self.assertEqual(record["action"], "finding")
            self.assertEqual(record["ts"], OBSERVED)
            self.assertNotIn("stage", record)
            self.assertEqual(record["build"]["kind"], lm.BACKFILL_KIND)
            self.assertEqual(record["build"]["manifest_sha256"], digest)
            self.assertEqual(record["build"]["observed_at"], OBSERVED)
            self.assertIn("no history before the observation", record["reason"])
        self.assertEqual(records[0]["build"]["provenance"],
                         [{"ref": "/archive/easybuild/legacy-1.0.log", "kind": "easybuild-log", "sha256": "cd" * 32}])
        self.assertEqual(records[0]["modulefile"], {"path": "/modules/all/legacy/1.0.lua", "sha256": "ab" * 32})
        self.assertEqual(records[1]["build"]["note"], "found during audit")
        for index in range(3):
            self.assertEqual(self.verify_record(index).returncode, 0)

    def test_repeated_retries_append_nothing(self):
        manifest, digest = self.write_manifest()
        first = self.backfill(manifest, digest)
        before = self.log_bytes()
        for clock in (DAY1 + 1, DAY2, DAY3):
            again = lm.backfill(self.eb, self.log, self.key, self.head, self.pub, manifest, digest,
                                timeout=60, clock=lambda clock=clock: clock)
            self.assertEqual((again["appended"], again["replayed"]), (0, 3))
            self.assertEqual([receipt["id"] for receipt in again["receipts"]],
                             [receipt["id"] for receipt in first["receipts"]])
            self.assertEqual(self.log_bytes(), before)

    def test_an_interrupted_backfill_resumes_without_duplicates(self):
        manifest, digest = self.write_manifest()
        second = lm.backfill_records(*lm.load_manifest(manifest, digest, DAY1))[1]["build"]["event_id"]
        with self.assertRaises(InjectedCrash):
            self.backfill(manifest, digest, faults=FaultInjector(f"backfill.append:{second}"))
        self.assertEqual(len(self.records()), 1)
        result = self.backfill(manifest, digest)
        self.assertEqual((result["appended"], result["replayed"]), (2, 1))
        self.assertEqual(len(self.records()), 3)
        self.assertEqual(len({record["build"]["event_id"] for record in self.records()}), 3)

    def test_a_torn_tail_is_quarantined_by_eb_stack_before_the_backfill(self):
        self.append("demo/1", "install")
        with open(self.log, "ab") as handle:
            handle.write(b'{"action":"inst')
        manifest, digest = self.write_manifest()
        self.backfill(manifest, digest)
        self.assertEqual(len(self.records()), 4)
        quarantined = [name for name in os.listdir(self.path("ledger")) if ".quarantine." in name]
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(self.verify_record(3).returncode, 0)

    def test_changed_manifest_content_is_refused_before_anything_is_appended(self):
        manifest, digest = self.write_manifest()
        self.backfill(manifest, digest)
        before = self.log_bytes()
        changed, changed_digest = self.write_manifest(observations=[
            {"module": "legacy/1.0", "location": "/sw/legacy/1.0", "provenance": [{"ref": "a different source"}]},
            {"module": "new/9", "location": "/sw/new/9", "provenance": [{"ref": "x"}]},
        ], name="changed.json")
        with self.assertRaisesRegex(Refused, "immutable"):
            self.backfill(changed, changed_digest)
        self.assertEqual(self.log_bytes(), before)
        with self.assertRaisesRegex(Refused, "not the pinned"):
            self.backfill(changed, digest)
        self.assertEqual(self.log_bytes(), before)

    def test_an_event_recorded_with_other_content_is_refused_before_anything_is_appended(self):
        manifest, digest = self.write_manifest()
        record = lm.backfill_records(*lm.load_manifest(manifest, digest, DAY1))[2]
        self.append("old/3", "finding", ts=OBSERVED, build={"event_id": record["build"]["event_id"]})
        before = self.log_bytes()
        with self.assertRaisesRegex(Refused, "other content"):
            self.backfill(manifest, digest)
        self.assertEqual(self.log_bytes(), before)

    def test_unpinned_or_guessed_input_is_refused(self):
        manifest, _digest = self.write_manifest()
        with self.assertRaisesRegex(Refused, "not the pinned"):
            self.backfill(manifest, "0" * 64)
        cases = [
            ({"observed_at": "2027-01-01T00:00:00Z"}, "after the current time"),
            ({"observed_at": "2026-11-02"}, "observed_at"),
            ({"observed_at": "2026-11-02T09:30:00+01:00"}, "observed_at"),
            ({"observations": [{"module": "a/1", "location": "/sw/a/1", "installed_at": "2019-01-01T00:00:00Z",
                                "provenance": [{"ref": "r"}]}]}, "unknown fields installed_at"),
            ({"observations": [{"module": "a/1", "location": "/sw/a/1", "action": "install",
                                "provenance": [{"ref": "r"}]}]}, "unknown fields action"),
            ({"observations": [{"module": "a/1", "location": "/sw/a/1", "provenance": []}]}, "original reference"),
            ({"observations": [{"module": "a/1", "location": "sw/a/1", "provenance": [{"ref": "r"}]}]}, "absolute"),
            ({"observations": [{"module": "a/1", "location": "/sw/a/1", "provenance": [{"ref": "r", "date": "x"}]}]},
             "unknown fields date"),
            ({"observations": [{"module": "a/1", "location": "/sw/a/1", "provenance": [{"ref": "r"}]}] * 2},
             "repeats"),
            ({"stage": "finished"}, "unknown fields stage"),
            ({"schema": "other"}, "schema"),
        ]
        for overrides, message in cases:
            with self.subTest(message=message):
                path, digest = self.write_manifest(name="case.json", **overrides)
                with self.assertRaisesRegex(Refused, message):
                    self.backfill(path, digest)
        self.assertFalse(os.path.exists(self.log))

    def test_path_aliases_and_a_key_beside_the_log_are_refused(self):
        manifest, digest = self.write_manifest()
        os.symlink(self.path("ledger"), self.path("alias"))
        with self.assertRaisesRegex(Refused, "aliases are refused"):
            lm.backfill(self.eb, self.path("alias", "stack.jsonl"), self.key, self.head, self.pub, manifest, digest,
                        clock=lambda: DAY1)
        with self.assertRaisesRegex(Refused, "normalised"):
            lm.backfill(self.eb, self.path("ledger", "..", "ledger", "stack.jsonl"), self.key, self.head, self.pub,
                        manifest, digest, clock=lambda: DAY1)
        beside = self.path("ledger", "key.pem")
        shutil.copy(self.key, beside)
        with self.assertRaisesRegex(Refused, "inside the ledger directory"):
            lm.backfill(self.eb, self.log, beside, self.head, self.pub, manifest, digest, clock=lambda: DAY1)
        with self.assertRaisesRegex(Refused, "same file"):
            lm.backfill(self.eb, self.log, self.key, self.key, self.pub, manifest, digest, clock=lambda: DAY1)
        self.assertFalse(os.path.exists(self.log))


class AnchorTests(RealLedger):
    def anchor_files(self):
        return sorted(name for name in os.listdir(self.anchors) if name.endswith(".head.json"))

    def test_the_first_anchor_is_read_only_single_link_and_verifies(self):
        self.append("demo/1", "install")
        result = self.anchor()
        self.assertEqual(result["status"], "published")
        self.assertEqual(self.anchor_files(), ["2026-11-03.head.json"])
        info = os.lstat(result["path"])
        self.assertEqual((stat.S_IMODE(info.st_mode), info.st_nlink), (0o444, 1))
        with open(result["path"], "rb") as handle:
            head = lm.parse_head(handle.read())
        self.assertEqual((head["size"], head["ts"]), (1, "2026-11-03T12:00:00Z"))
        self.assertEqual(self.verify_record(0, head=result["path"]).returncode, 0)
        self.assertEqual(lm.verify_anchors(self.eb, self.log, self.pub, self.anchors)["anchors"], 1)
        self.assertFalse([name for name in os.listdir(self.anchors) if name.startswith(".anchor-staging-")])

    def test_an_empty_log_can_be_anchored(self):
        open(self.log, "w").close()
        self.assertEqual(self.anchor()["size"], 0)
        self.append("demo/1", "install")
        self.assertEqual(self.anchor(clock=DAY2)["size"], 1)
        self.assertEqual(lm.verify_anchors(self.eb, self.log, self.pub, self.anchors)["anchors"], 2)

    def test_a_same_day_retry_never_replaces_the_anchor(self):
        self.append("demo/1", "install")
        first = self.anchor()
        info = os.lstat(first["path"])
        with open(first["path"], "rb") as handle:
            before = handle.read()
        self.append("demo/2", "install")
        for _ in range(2):
            again = self.anchor(clock=DAY1 + 3600)
            self.assertEqual((again["status"], again["size"], again["root"]), ("exists", 1, first["root"]))
        with open(first["path"], "rb") as handle:
            self.assertEqual(handle.read(), before)
        self.assertEqual(os.lstat(first["path"]).st_ino, info.st_ino)
        self.assertEqual(self.anchor_files(), ["2026-11-03.head.json"])

    def test_daily_anchors_form_a_verified_chain(self):
        self.append("demo/1", "install")
        self.anchor()
        self.append("demo/2", "install")
        self.append("demo/3", "install")
        second = self.anchor(clock=DAY2)
        self.assertEqual((second["status"], second["size"]), ("published", 3))
        third = self.anchor(clock=DAY3)
        self.assertEqual((third["status"], third["size"]), ("published", 3))
        self.cli("verify", "--log", self.log, "--public-key", self.pub,
                 "--from", os.path.join(self.anchors, "2026-11-03.head.json"), "--to", second["path"])
        result = lm.verify_anchors(self.eb, self.log, self.pub, self.anchors)
        self.assertEqual((result["anchors"], result["size"]), (3, 3))

    def test_a_rewritten_log_is_refused_and_no_anchor_is_added(self):
        self.append("demo/1", "install")
        self.append("demo/2", "install")
        self.anchor()
        other = self.path("work", "rewritten.jsonl")
        self.append("demo/1", "install", log=other, head=self.path("work", "other-head.json"), location="/sw/evil")
        for number in range(3):
            self.append(f"demo/{number + 2}", "install", log=other, head=self.path("work", "other-head.json"))
        with open(other, "rb") as source, open(self.log, "wb") as target:
            target.write(source.read())
        with self.assertRaises(lm.Equivocation):
            self.anchor(clock=DAY2)
        self.assertEqual(self.anchor_files(), ["2026-11-03.head.json"])
        with self.assertRaises(lm.Equivocation):
            self.anchor(clock=DAY1 + 60)
        with self.assertRaises(Refused):
            lm.verify_anchors(self.eb, self.log, self.pub, self.anchors)

    def test_a_truncated_log_is_refused(self):
        for number in range(3):
            self.append(f"demo/{number}", "install")
        self.anchor()
        data = self.log_bytes()
        with open(self.log, "wb") as handle:
            handle.write(data[: data.index(b"\n") + 1])
        with self.assertRaises(lm.Equivocation):
            self.anchor(clock=DAY2)
        self.assertEqual(self.anchor_files(), ["2026-11-03.head.json"])

    def test_a_tampered_or_malformed_anchor_is_refused_and_kept(self):
        self.append("demo/1", "install")
        published = self.anchor()["path"]
        self.append("demo/2", "install")
        with open(published, "rb") as handle:
            good = handle.read()
        head = json.loads(good)
        cases = {
            "signature": dict(head, signature=("0" if head["signature"][0] != "0" else "1") + head["signature"][1:]),
            "root": dict(head, root="0" * 64),
        }
        for name, tampered in cases.items():
            with self.subTest(name=name):
                os.chmod(published, 0o644)
                with open(published, "w") as handle:
                    handle.write(json.dumps(tampered, separators=(",", ":")) + "\n")
                with self.assertRaises(Refused):
                    self.anchor(clock=DAY2)
                self.assertEqual(self.anchor_files(), ["2026-11-03.head.json"])
        for name, data in {"truncated": good[:40], "extra field": good[:-2] + b',"x":1}\n'}.items():
            with self.subTest(name=name):
                with open(published, "wb") as handle:
                    handle.write(data)
                with self.assertRaises(Refused):
                    self.anchor(clock=DAY2)
                with open(published, "rb") as handle:
                    self.assertEqual(handle.read(), data)
                self.assertEqual(self.anchor_files(), ["2026-11-03.head.json"])

    def test_an_anchor_from_a_later_day_or_another_key_is_refused(self):
        self.append("demo/1", "install")
        self.anchor(clock=DAY2)
        with self.assertRaisesRegex(Refused, "after 2026-11-03"):
            self.anchor(clock=DAY1)
        other_key, other_pub = self.path("keys", "other.pem"), self.path("keys", "other.pub")
        self.genkey(other_key, other_pub)
        with self.assertRaises(Refused):
            self.anchor(clock=DAY3, key=other_key)
        self.assertEqual(self.anchor_files(), ["2026-11-04.head.json"])

    def test_interrupted_publication_leaves_no_partial_anchor(self):
        self.append("demo/1", "install")
        for step in ("anchor.sign", "anchor.publish"):
            with self.subTest(step=step):
                with self.assertRaises(InjectedCrash):
                    self.anchor(faults=FaultInjector(step))
                self.assertEqual(self.anchor_files(), [])
        with self.assertRaises(InjectedCrash):
            self.anchor(faults=FaultInjector("anchor.published"))
        self.assertEqual(self.anchor_files(), ["2026-11-03.head.json"])
        self.assertEqual(self.anchor()["status"], "exists")
        self.assertEqual(lm.verify_anchors(self.eb, self.log, self.pub, self.anchors)["anchors"], 1)

    def test_a_crash_between_link_and_cleanup_is_recovered(self):
        self.append("demo/1", "install")
        published = self.anchor()["path"]
        leftover = os.path.join(self.anchors, ".anchor-staging-crashed")
        os.makedirs(leftover)
        os.link(published, os.path.join(leftover, "head.json"))
        with open(os.path.join(leftover, "ledger.jsonl"), "wb") as handle:
            handle.write(b'{"partial')
        with self.assertRaisesRegex(Refused, "one link"):
            lm.verify_anchors(self.eb, self.log, self.pub, self.anchors)
        self.assertEqual(self.anchor()["status"], "exists")
        self.assertFalse(os.path.exists(leftover))
        self.assertEqual(os.lstat(published).st_nlink, 1)

    def test_an_anchor_with_another_name_is_refused(self):
        self.append("demo/1", "install")
        published = self.anchor()["path"]
        os.link(published, self.path("work", "copy.json"))
        with self.assertRaisesRegex(Refused, "one link"):
            self.anchor(clock=DAY2)
        os.unlink(self.path("work", "copy.json"))
        os.rename(published, os.path.join(self.anchors, "2026-11-02.head.json"))
        with self.assertRaisesRegex(Refused, "not on 2026-11-02"):
            self.anchor(clock=DAY2)

    def test_an_index_checkpoint_line_after_the_head_receipt_is_accepted(self):
        self.append("demo/1", "install")
        indexed = self.wrapper(
            'if argv[:2] == ["ledger", "head"]:\n'
            '    out += "index checkpoint eb-stack-ledger/demo/2026/index size 1 root " + "0" * 64 + " to /x\\n"\n'
        )
        self.assertEqual(self.anchor(eb=indexed)["status"], "published")
        self.assertEqual(self.anchor_files(), ["2026-11-03.head.json"])

    def test_malformed_head_receipts_are_refused(self):
        self.append("demo/1", "install")
        noisy = self.wrapper('if argv[:2] == ["ledger", "head"]:\n    out += "extra\\n"\n')
        with self.assertRaisesRegex(PublisherError, "unexpected receipt"):
            self.anchor(eb=noisy)
        lying = self.wrapper('if argv[:2] == ["ledger", "verify"]:\n    out = out.replace("size 1", "size 2")\n')
        with self.assertRaisesRegex(lm.VerificationError, "unexpected receipt"):
            self.anchor(eb=lying)
        failing = self.wrapper('if argv[:2] == ["ledger", "verify"]:\n    code, out = 1, ""\n')
        with self.assertRaises(lm.VerificationError):
            self.anchor(eb=failing)
        self.assertEqual(self.anchor_files(), [])

    def test_anchor_paths_are_checked(self):
        self.append("demo/1", "install")
        with self.assertRaisesRegex(Refused, "must not hold the ledger"):
            self.anchor(anchors=self.path("ledger"))
        inside = self.path("anchors", "key.pem")
        shutil.copy(self.key, inside)
        with self.assertRaisesRegex(Refused, "inside the anchor directory"):
            self.anchor(key=inside)
        os.unlink(inside)
        os.symlink(self.anchors, self.path("anchors-alias"))
        with self.assertRaisesRegex(Refused, "aliases are refused"):
            self.anchor(anchors=self.path("anchors-alias"))
        os.link(self.log, self.path("work", "log-alias"))
        with self.assertRaisesRegex(Refused, "hard links"):
            self.anchor()
        self.assertEqual(self.anchor_files(), [])


class ProjectionTests(RealLedger):
    def history(self):
        """A log with every kind of event the projection interprets."""
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
        manifest, digest = self.write_manifest()
        self.backfill(manifest, digest)
        return rollback

    def package(self, name, out=None):
        with open(os.path.join(out or self.out, "packages", f"{name}.json")) as handle:
            return json.load(handle)

    def test_history_keeps_correction_retire_access_and_finding_semantics(self):
        rollback = self.history()
        result = self.project()
        self.assertEqual((result["size"], result["packages"]), (14, 5))
        demo = self.package("demo")
        events = {event["index"]: event for event in demo["events"]}
        self.assertEqual(events[3]["status"], "corrected")
        self.assertEqual(events[3]["corrected_by"], [5])
        self.assertEqual(events[3]["correction_pending_by"], [4])
        self.assertEqual(events[5]["links"]["corrects"], {"id": events[3]["id"], "index": 3})
        self.assertEqual(demo["modules"], [{"module": "demo/1", "state": "installed", "location": "/sw/demo/1",
                                            "last_change": 1, "access": [6], "findings": []}])
        self.assertEqual(events[6]["reason"], {"change": "granted", "group": "hpc"})
        tool = self.package("tool")
        self.assertEqual(tool["modules"][0]["state"], "retired")
        self.assertEqual(tool["events"][1]["status"], "effective")
        self.assertEqual(tool["events"][1]["corrected_by"], [])
        zlib = self.package("zlib")
        self.assertEqual((zlib["modules"][0]["state"], zlib["modules"][0]["findings"]), ("observed", [10]))
        legacy = self.package("legacy")
        self.assertEqual(legacy["modules"][0]["state"], "observed")
        self.assertTrue(legacy["events"][0]["observed"])
        self.assertEqual(legacy["events"][0]["provenance"][0]["ref"], "/archive/easybuild/legacy-1.0.log")
        self.assertEqual(legacy["events"][0]["ts"], OBSERVED)
        with open(os.path.join(self.out, "packages", "demo.md")) as handle:
            markdown = handle.read()
        self.assertIn("corrected by \\#5", markdown)
        self.assertIn("correction started by \\#4", markdown)
        with open(os.path.join(self.out, "index.json")) as handle:
            index = json.load(handle)
        self.assertEqual([entry["name"] for entry in index["packages"]], ["demo", "legacy", "old", "tool", "zlib"])
        self.assertEqual(index["head"]["size"], 14)

        self.append("demo/1", "correction", corrects=rollback["id"], reason="the rollback was itself wrong")
        self.project()
        demo = self.package("demo")
        events = {event["index"]: event for event in demo["events"]}
        self.assertEqual((events[5]["status"], events[3]["status"]), ("corrected", "effective"))
        self.assertEqual(demo["modules"][0]["state"], "installed")
        self.assertEqual((demo["modules"][0]["last_change"], demo["modules"][0]["location"]), (3, "/sw/demo/1-gen2"))

    def test_projection_is_deterministic(self):
        self.history()
        self.project(out=self.path("one"))
        self.project(out=self.path("two"))
        self.project(out=self.path("one"))
        first, second = tree(self.path("one")), tree(self.path("two"))
        self.assertEqual(first, second)
        self.assertEqual(sorted(first), [
            "index.json", "index.md", "packages/demo.json", "packages/demo.md", "packages/legacy.json",
            "packages/legacy.md", "packages/old.json", "packages/old.md", "packages/tool.json", "packages/tool.md",
            "packages/zlib.json", "packages/zlib.md",
        ])

    def test_untrusted_text_is_escaped(self):
        hostile = "evil|pkg<script>alert(1)<\\script>`x`*[link](javascript:e)\\|"
        self.append(f"{hostile}/1", "install", actor="a\n# injected heading\n| x | y |", reason="\u202etxt.exe & <b>")
        self.append("../../../etc/1", "install")
        self.append("café/1", "access", reason="ok")
        self.sign()
        self.project()
        files = tree(self.out)
        for name in files:
            self.assertTrue(os.path.realpath(os.path.join(self.out, name)).startswith(self.out + os.sep))
        markdown = b"".join(data for name, data in files.items() if name.endswith(".md")).decode("utf-8")
        self.assertNotIn("<script>", markdown)
        self.assertNotIn("<b>", markdown)
        self.assertNotIn("\u202e", markdown)
        self.assertNotIn("\n# injected", markdown)
        self.assertIn("U+202E", markdown)
        for name, data in files.items():
            if not name.endswith(".md"):
                json.loads(data)
                self.assertTrue(data.isascii())
                continue
            header = None
            for line in data.decode("utf-8").splitlines():
                if not line.startswith("|"):
                    header = None
                    continue
                if header is None:
                    header = cell_separators(line)
                self.assertEqual(cell_separators(line), header, f"{name}: {line}")
        evil = [name for name in files if name.startswith("packages/evil")]
        self.assertEqual(len(evil), 2)
        document = json.loads(files[evil[0] if evil[0].endswith(".json") else evil[1]])
        self.assertEqual(document["package"], hostile)
        self.assertEqual(document["events"][0]["actor"], "a\n# injected heading\n| x | y |")
        index = files["index.md"].decode("utf-8")
        self.assertIn("(packages/evil%257Cpkg", index)

    def test_a_tampered_log_is_refused_and_nothing_is_written(self):
        for number in range(3):
            self.append(f"demo/{number}", "install")
        good = self.log_bytes()
        edited = good.replace(b"/sw/demo/1", b"/sw/demo/X", 1)
        with open(self.log, "wb") as handle:
            handle.write(edited)
        with self.assertRaises(lm.VerificationError):
            self.project()
        other = self.path("work", "rewritten.jsonl")
        for number in range(3):
            self.append(f"demo/{number}", "install", log=other, head=self.path("work", "other-head.json"),
                        location=f"/sw/rewritten/{number}")
        with open(other, "rb") as source, open(self.log, "wb") as target:
            target.write(source.read())
        with self.assertRaisesRegex(lm.VerificationError, "is not in the head"):
            self.project()
        with open(self.log, "wb") as handle:
            handle.write(good + b'{"partial')
        with self.assertRaisesRegex(Refused, "partial write"):
            self.project()
        self.assertFalse(os.path.exists(self.out))

    def test_a_tampered_or_foreign_head_is_refused(self):
        for number in range(2):
            self.append(f"demo/{number}", "install")
        with open(self.head) as handle:
            head = json.loads(handle.read())
        other_key, other_pub = self.path("keys", "other.pem"), self.path("keys", "other.pub")
        self.genkey(other_key, other_pub)
        foreign = self.path("heads", "foreign.json")
        self.cli("head", "--log", self.log, "--key", other_key, "--out", foreign)
        cases = {
            "root": json.dumps(dict(head, root="1" * 64), separators=(",", ":")) + "\n",
            "size": json.dumps(dict(head, size=1), separators=(",", ":")) + "\n",
            "ts": json.dumps(dict(head, ts="2030-01-01T00:00:00Z"), separators=(",", ":")) + "\n",
            "domain": json.dumps(dict(head, domain="other-log"), separators=(",", ":")) + "\n",
            "spaced": json.dumps(head) + "\n",
            "not json": "{",
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                path = self.path("heads", f"{name.replace(' ', '-')}.json")
                with open(path, "w") as handle:
                    handle.write(text)
                with self.assertRaises(Refused):
                    self.project(head=path)
        with self.assertRaisesRegex(lm.VerificationError, "not by the trusted key"):
            self.project(head=foreign)
        self.assertFalse(os.path.exists(self.out))
        self.project()

    def test_a_stale_head_is_refused(self):
        self.append("demo/1", "install")
        stale = self.path("heads", "stale.json")
        shutil.copy(self.head, stale)
        self.append("demo/2", "install")
        with self.assertRaises(lm.StaleHead):
            self.project(head=stale)
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(self.project()["size"], 2)

    def test_an_interrupted_projection_is_completed_by_a_rerun(self):
        self.history()
        self.project(out=self.path("clean"))
        with self.assertRaises(InjectedCrash):
            self.project(faults=FaultInjector("projection.packages/old.md.rename"))
        self.assertFalse(os.path.exists(os.path.join(self.out, "index.json")))
        self.project()
        self.assertEqual(tree(self.out), tree(self.path("clean")))
        self.append("demo/1", "access", reason="later")
        with self.assertRaises(InjectedCrash):
            self.project(faults=FaultInjector("projection.index.json.fsync"))
        with open(os.path.join(self.out, "index.json")) as handle:
            self.assertEqual(json.load(handle)["head"]["size"], 14)
        self.project()
        with open(os.path.join(self.out, "index.json")) as handle:
            self.assertEqual(json.load(handle)["head"]["size"], 15)

    def test_output_directory_is_checked(self):
        self.append("demo/1", "install")
        os.makedirs(self.out)
        with open(os.path.join(self.out, "notes.txt"), "w") as handle:
            handle.write("mine")
        with self.assertRaisesRegex(Refused, "not a ledger history projection"):
            self.project()
        with self.assertRaisesRegex(Refused, "must not hold the ledger"):
            self.project(out=self.path("ledger"))
        os.symlink(self.path("work"), self.path("out-alias"))
        with self.assertRaisesRegex(Refused, "aliases are refused"):
            self.project(out=self.path("out-alias"))

    def test_a_malformed_verify_receipt_is_refused(self):
        self.append("demo/1", "install")
        silent = self.wrapper('if argv[:2] == ["ledger", "verify"]:\n    out = ""\n')
        with self.assertRaisesRegex(lm.VerificationError, "unexpected receipt"):
            self.project(eb=silent)
        doubled = self.wrapper('if argv[:2] == ["ledger", "verify"]:\n    out = out + out\n')
        with self.assertRaisesRegex(lm.VerificationError, "unexpected receipt"):
            self.project(eb=doubled)
        self.assertFalse(os.path.exists(self.out))


class CommandLineTests(RealLedger):
    def run_main(self, *argv):
        # TOOL_BIN runs these against another build of the tool, through its
        # ledger-maintenance subcommand.
        tool = os.environ.get("TOOL_BIN")
        command = [tool, "ledger-maintenance"] if tool else [sys.executable, "-m", "eb_staged_rebuild.ledger_maintenance"]
        done = subprocess.run([*command, *argv],
                              check=False, capture_output=True, text=True, timeout=120,
                              cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return done.returncode, done.stdout, done.stderr

    def test_backfill_anchor_and_project_from_the_command_line(self):
        manifest, digest = self.write_manifest(observed_at="2026-01-02T03:04:05Z")
        common = ["--eb-stack", self.eb, "--log", self.log, "--public-key", self.pub]
        code, out, err = self.run_main("backfill", *common, "--key", self.key, "--head", self.head,
                                       "--manifest", manifest, "--manifest-sha256", digest)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["appended"], 3)
        code, out, err = self.run_main("anchor", *common, "--key", self.key, "--anchors", self.anchors)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"], "published")
        code, out, err = self.run_main("verify-anchors", *common, "--anchors", self.anchors)
        self.assertEqual((code, json.loads(out)["anchors"]), (0, 1), err)
        code, out, err = self.run_main("project", *common, "--head", self.head, "--out", self.out)
        self.assertEqual((code, json.loads(out)["packages"]), (0, 2), err)
        self.append("demo/1", "install", head=self.path("work", "newer.json"))
        code, _out, err = self.run_main("project", *common, "--head", self.head, "--out", self.out)
        self.assertEqual(code, 2)
        self.assertIn("sign a current head", err)
        hanging = self.wrapper('if argv[:2] == ["ledger", "verify"]:\n    import time; time.sleep(30)\n')
        code, _out, err = self.run_main("project", "--eb-stack", hanging, "--log", self.log, "--public-key",
                                        self.pub, "--head", self.path("work", "newer.json"), "--out", self.out,
                                        "--timeout", "1")
        self.assertEqual(code, 75, err)


class InputTests(unittest.TestCase):
    def test_markdown_escaping(self):
        self.assertEqual(lm.md("a|b"), "a\\|b")
        self.assertEqual(lm.md("<i>&"), "&lt;i&gt;&amp;")
        self.assertEqual(lm.md("x\ny\r\u2028\u202e\x1b"), "xU+000AyU+000DU+2028U+202EU+001B")
        self.assertEqual(lm.md("`*_[]()~!#\\"), "\\`\\*\\_\\[\\]\\(\\)\\~\\!\\#\\\\")
        self.assertEqual(lm.md({"b": 1, "a": "<"}), '{"a":"&lt;","b":1}')
        self.assertEqual(lm.md(None), "")

    def test_file_stems_stay_in_one_directory(self):
        for name in ("..", ".", "", "a/b", "x" * 400, "a\x00b"):
            stem = lm.file_stem(name)
            self.assertNotIn("/", stem)
            self.assertNotIn(stem, ("", ".", ".."))
            self.assertLessEqual(len(stem), 150)
        self.assertNotEqual(lm.file_stem("x" * 400), lm.file_stem("x" * 401))
        self.assertEqual(lm.file_stem("PETSc"), "PETSc")

    def test_heads_must_be_exactly_what_eb_stack_writes(self):
        head = {"domain": lm.DOMAIN, "size": 1, "root": "a" * 64, "ts": "2026-11-03T12:00:00Z",
                "public_key": "b" * 64, "signature": "c" * 128}
        data = (json.dumps(head, separators=(",", ":")) + "\n").encode()
        self.assertEqual(lm.parse_head(data), head)
        bad = [
            data[:-1], json.dumps(head).encode() + b"\n",
            (json.dumps(dict(head, size=True), separators=(",", ":")) + "\n").encode(),
            (json.dumps(dict(head, size=-1), separators=(",", ":")) + "\n").encode(),
            (json.dumps(dict(head, root="A" * 64), separators=(",", ":")) + "\n").encode(),
            (json.dumps(dict(head, ts="2026-11-03T12:00:00+00:00"), separators=(",", ":")) + "\n").encode(),
            (json.dumps({"size": 1, **head}, separators=(",", ":")) + "\n").encode(),
            data.replace(b'"size":1', b'"size":1.0'),
            data.replace(b'"size":1', b'"size":1,"size":1'),
        ]
        for case in bad:
            with self.subTest(case=case), self.assertRaises(Refused):
                lm.parse_head(case)

    def test_log_lines_must_be_canonical_ledger_records(self):
        record = {"action": "install", "actor": "a", "id": "f" * 64, "index": 0, "location": "/x",
                  "module": "m/1", "stack": "s", "target": "t", "ts": "2026-11-03T12:00:00Z"}
        line = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        self.assertEqual(lm.parse_log(line), [record])
        bad = [
            line[:-1],
            json.dumps(record).encode() + b"\n",
            line.replace(b'"index":0', b'"index":1'),
            line.replace(b'"action":"install"', b'"action":"reinstall"'),
            line.replace(b'"actor":"a",', b'"actor":"a","licence":"x",'),
            line + line.replace(b'"stack":"s"', b'"stack":"other"').replace(b'"index":0', b'"index":1'),
            line.replace(b'"index":0', b'"index":0.0'),
            b"\xff\n",
        ]
        for case in bad:
            with self.subTest(case=case), self.assertRaises(Refused):
                lm.parse_log(case)


if __name__ == "__main__":
    unittest.main()

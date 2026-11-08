"""Readers and maintenance tools for an eb-stack ledger.

Three local file tools over the log that ``eb-stack ledger append`` writes:

* ``backfill`` appends ``finding`` records for software observed at an
  explicit time, from an operator-written manifest pinned by its SHA-256.
* ``anchor`` signs the day's head with ``eb-stack ledger head`` and adds it
  to a directory of immutable daily anchors after ``eb-stack ledger verify``
  proves it extends the latest anchor.
* ``project`` writes per-package JSON and Markdown history from a log whose
  head ``eb-stack ledger verify`` accepted.

Every signature comes from ``eb-stack ledger head`` or ``ledger append`` and
every proof from ``eb-stack ledger verify``, run through the adapter's
bounded runner. The log and head files are read here only to refuse bad
input early and to bind what is projected to the bytes the CLI verified:
the bytes are read once, copied into a private directory, verified there,
and parsed from the same buffer.
"""

import argparse
import calendar
import errno
import hashlib
import itertools
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import urllib.parse

from eb_staged_rebuild.atomicio import atomic_write, fsync_dir
from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.faults import FaultInjector
from eb_staged_rebuild.ledger_adapter import EbStackLedger, _bounded
from eb_staged_rebuild.lockfile import FileLock
from eb_staged_rebuild.outbox import PENDING_EXIT, PublisherError

DOMAIN = "eb-stack-ledger-v1"
MANIFEST_SCHEMA = "eb-staged-rebuild/observed-backfill/1"
HISTORY_SCHEMA = "eb-staged-rebuild/ledger-history/1"
BACKFILL_KIND = "observed-backfill"
TOOL = "eb-staged-rebuild/ledger-maintenance"
DEFAULT_TIMEOUT = 120

ACTIONS = (
    "install", "rebuild-inplace", "rebuild-bump", "retire", "unretire",
    "move", "access", "remove", "finding", "correction",
)
STAGES = ("started", "finished", "aborted")
_REQUIRED = ("ts", "actor", "stack", "target", "module", "location", "action")
_LINKS = ("supersedes", "superseded_by", "moved_from", "moved_to", "corrects")
_STRUCTURED = ("reason", "easyconfig", "sources", "sbom", "modulefile", "build")
_KNOWN = frozenset(_REQUIRED + _LINKS + _STRUCTURED + ("id", "index", "sig", "stage"))
_HEAD_FIELDS = ("domain", "size", "root", "ts", "public_key", "signature")

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX128 = re.compile(r"^[0-9a-f]{128}$")
_HEAD_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z$")
_OBSERVED_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_MANIFEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")
_ANCHOR_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})\.head\.json$")
# The line eb-stack prints after a verify receipt: the tile log beside the
# ledger extends the head it checked.
_TILE_LOG = re.compile(r"^tile log size (\d+) root ([0-9a-f]{64}) extends the head and holds the same lines$")
# The line eb-stack prints after a head receipt: the checkpoint it wrote.
_CHECKPOINT = re.compile(r"^checkpoint \S+ size (\d+) root ([0-9a-f]{64}) to .+$")
_STAGING_PREFIX = ".anchor-staging-"


class VerificationError(Refused):
    """``eb-stack ledger verify`` did not accept the log, a head or a proof."""


class StaleHead(VerificationError):
    """The signed head covers fewer records than the log holds."""


class Equivocation(Refused):
    """Two signed heads disagree about the same history."""


def _utc(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(seconds)))


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


# Paths


def _plain_path(path, what):
    """``path`` as given, refused unless absolute, normalised and symlink-free."""
    if not isinstance(path, str) or not path:
        raise Refused(f"{what} must be a path")
    if not os.path.isabs(path) or os.path.normpath(path) != path or path.startswith("//"):
        raise Refused(f"{what} {path!r} must be an absolute, normalised path")
    real = os.path.realpath(path)
    if real != path:
        raise Refused(f"{what} {path} resolves to {real}; path aliases are refused")
    return path


def _existing_file(path, what, single_link=True):
    _plain_path(path, what)
    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise Refused(f"{what} {path} does not exist") from exc
    if not stat.S_ISREG(info.st_mode):
        raise Refused(f"{what} {path} is not a regular file")
    if single_link and info.st_nlink != 1:
        raise Refused(f"{what} {path} has {info.st_nlink} hard links; aliases are refused")
    return path


def _new_or_file(path, what):
    _plain_path(path, what)
    parent = os.path.dirname(path)
    if not os.path.isdir(parent) or os.path.islink(parent):
        raise Refused(f"the directory of {what} {path} does not exist")
    if os.path.lexists(path):
        _existing_file(path, what)
    return path


def _existing_dir(path, what):
    _plain_path(path, what)
    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise Refused(f"{what} {path} does not exist") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise Refused(f"{what} {path} is not a directory")
    return path


def _inside(child, parent):
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def _distinct_files(named):
    """Refuse two roles that name one file, by path or by inode."""
    seen = {}
    for what, path in named.items():
        if path is None:
            continue
        try:
            info = os.stat(path)
            identity = (info.st_dev, info.st_ino)
        except FileNotFoundError:
            identity = ("missing", path)
        if identity in seen:
            raise Refused(f"{what} {path} is the same file as {seen[identity]}")
        seen[identity] = what


def _key_outside(key, directories):
    for what, directory in directories.items():
        if directory is not None and _inside(key, directory):
            raise Refused(f"signing key {key} is inside the {what} {directory}; keep the key off that tree")


def _executable(path):
    _existing_file(path, "eb-stack executable", single_link=False)
    if not os.access(path, os.X_OK):
        raise Refused(f"eb-stack executable {path} is not executable")
    return path


def _read_regular(path, what):
    """The bytes of a regular file opened without following a symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise Refused(f"{what} {path} is a symbolic link") from exc
        raise Refused(f"cannot open {what} {path}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refused(f"{what} {path} is not a regular file")
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def _write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


# Log and head readers


def _strict_json(text, what):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Refused(f"{what} repeats the key {key!r}")
            result[key] = value
        return result

    def no_float(token):
        raise Refused(f"{what} holds the non-integer number {token}")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_float=no_float, parse_constant=no_float)
    except ValueError as exc:
        raise Refused(f"{what} is not JSON: {exc}") from exc


def _canonical(value):
    """The ledger's canonical JSON: keys in code point (UTF-8 byte) order, no whitespace."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _non_empty(value):
    return isinstance(value, str) and value != ""


def parse_head(data, what="head"):
    """A signed head file exactly as ``eb-stack`` writes it."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Refused(f"{what} is not UTF-8") from exc
    head = _strict_json(text, what)
    if not isinstance(head, dict) or tuple(head) != _HEAD_FIELDS:
        raise Refused(f"{what} must hold exactly {', '.join(_HEAD_FIELDS)}, in that order")
    if head["domain"] != DOMAIN:
        raise Refused(f"{what} domain is {head['domain']!r}, not {DOMAIN}")
    size = head["size"]
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise Refused(f"{what} size must be a non-negative integer")
    if not isinstance(head["root"], str) or not _HEX64.match(head["root"]):
        raise Refused(f"{what} root must be 64 lower-case hex characters")
    if not isinstance(head["public_key"], str) or not _HEX64.match(head["public_key"]):
        raise Refused(f"{what} public_key must be 64 lower-case hex characters")
    if not isinstance(head["signature"], str) or not _HEX128.match(head["signature"]):
        raise Refused(f"{what} signature must be 128 lower-case hex characters")
    if not isinstance(head["ts"], str) or not _HEAD_TS.match(head["ts"]):
        raise Refused(f"{what} ts must be RFC 3339 UTC ending in Z")
    if json.dumps(head, separators=(",", ":")) + "\n" != text:
        raise Refused(f"{what} is not in the form eb-stack writes")
    return head


def _check_record(record, index, stack):
    for key in record:
        if key not in _KNOWN:
            raise Refused(f"ledger line {index} has the unknown field {key!r}")
    for key in _REQUIRED:
        if not _non_empty(record.get(key)):
            raise Refused(f"ledger line {index} field {key} must be a non-empty string")
    if record["action"] not in ACTIONS:
        raise Refused(f"ledger line {index} action {record['action']!r} is not a ledger action")
    for key in _LINKS + ("stage", "sig"):
        if key in record and not _non_empty(record[key]):
            raise Refused(f"ledger line {index} field {key} must be a non-empty string")
    if "stage" in record and record["stage"] not in STAGES:
        raise Refused(f"ledger line {index} stage {record['stage']!r} is not a ledger stage")
    if record.get("index") != index or isinstance(record.get("index"), bool):
        raise Refused(f"ledger line {index} carries index {record.get('index')!r}")
    if not isinstance(record.get("id"), str) or not _HEX64.match(record["id"]):
        raise Refused(f"ledger line {index} id must be 64 lower-case hex characters")
    if record["action"] == "correction" and "corrects" not in record:
        raise Refused(f"ledger line {index} is a correction that names no record")
    build = record.get("build")
    if isinstance(build, dict) and "event_id" in build and not _non_empty(build["event_id"]):
        raise Refused(f"ledger line {index} build.event_id must be a non-empty string")
    if stack is not None and record["stack"] != stack:
        raise Refused(f"ledger line {index} is stack {record['stack']!r} in a log of stack {stack!r}")


def parse_log(data, what="ledger"):
    """Records of a ledger log, each line checked against the ledger format.

    ``eb-stack ledger verify`` remains the authority; this refuses early, and
    a log ending in a partial line is refused rather than repaired.
    """
    if not data:
        return []
    if not data.endswith(b"\n"):
        raise Refused(f"the last line of {what} has no newline; it ends in a partial write")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Refused(f"{what} is not UTF-8") from exc
    records = []
    for index, line in enumerate(text[:-1].split("\n")):
        record = _strict_json(line, f"{what} line {index}")
        if not isinstance(record, dict):
            raise Refused(f"{what} line {index} is not a JSON object")
        _check_record(record, index, records[0]["stack"] if records else None)
        if _canonical(record) != line:
            raise Refused(f"{what} line {index} is not in canonical form")
        records.append(record)
    return records


def _committed_prefix(data):
    """The bytes up to the last newline: a concurrent append may be mid-line."""
    end = data.rfind(b"\n")
    return data[: end + 1] if end >= 0 else b""


class _Verifier:
    """``eb-stack ledger verify`` against private copies of the bytes read."""

    def __init__(self, eb_stack, public_key, timeout):
        self.eb_stack = eb_stack
        self.public_key = public_key
        self.timeout = timeout

    def _run(self, argv, expected, size, root):
        """``expected`` is the receipt line; one tile-log line for ``size`` and ``root`` follows it."""
        code, stdout, stderr = _bounded([self.eb_stack, "ledger", "verify", *argv], self.timeout)
        if code != 0:
            raise VerificationError(f"eb-stack ledger verify exited {code}: {stderr.strip()[-500:]}")
        lines = stdout.splitlines(keepends=True)
        tiles = _TILE_LOG.match(lines[1].rstrip("\n")) if len(lines) == 2 else None
        if lines[:1] != [expected] or tiles is None or tiles.groups() != (str(size), root):
            raise VerificationError(f"eb-stack ledger verify printed an unexpected receipt: {stdout[-500:]!r}")

    def tiles(self, log):
        """Write the tile log beside a private copy of ``log``; verify audits it."""
        code, _stdout, stderr = _bounded([self.eb_stack, "ledger", "tiles", "--log", log], self.timeout)
        if code != 0:
            raise VerificationError(f"eb-stack ledger tiles exited {code}: {stderr.strip()[-500:]}")

    def binds(self, log, head_path, head, records):
        """The signed head is the root of the first ``head.size`` records of ``log``."""
        size, root = head["size"], head["root"]
        if size > len(records):
            raise VerificationError(f"head covers {size} records but the ledger has {len(records)}")
        if size == 0:
            self._run(
                ["--log", log, "--public-key", self.public_key, "--from", head_path, "--to", head_path],
                f"the head of size 0 root {root} extends the head of size 0 root {root}\n",
                size, root,
            )
            return
        last = size - 1
        self._run(
            ["--log", log, "--public-key", self.public_key, "--record", str(last), "--head", head_path],
            f"record {last} id {records[last]['id']} is in the head of size {size} root {root}\n",
            size, root,
        )

    def extends(self, log, older_path, older, newer_path, newer):
        if older["size"] == newer["size"] and older["root"] != newer["root"]:
            raise Equivocation(
                f"two signed heads of size {older['size']} have roots {older['root']} and {newer['root']}"
            )
        if older["size"] > newer["size"]:
            raise Equivocation(
                f"the head of size {newer['size']} is older than an anchored head of size {older['size']}"
            )
        try:
            self._run(
                ["--log", log, "--public-key", self.public_key, "--from", older_path, "--to", newer_path],
                f"the head of size {newer['size']} root {newer['root']} extends "
                f"the head of size {older['size']} root {older['root']}\n",
                newer["size"], newer["root"],
            )
        except VerificationError as exc:
            raise Equivocation(f"the head of size {newer['size']} does not extend the anchored head: {exc}") from exc


class VerifiedLog:
    """Records and the signed head ``eb-stack ledger verify`` accepted for them."""

    def __init__(self, head, records):
        self.head = head
        self.records = records


def read_verified(eb_stack, log, head, public_key, timeout=DEFAULT_TIMEOUT):
    """Read ``log`` and ``head`` once and have eb-stack verify those bytes.

    The head must be signed by ``public_key`` and cover every record: a head
    that covers fewer is stale and refused, so nothing unverified is read.
    """
    _executable(eb_stack)
    _existing_file(log, "ledger")
    _existing_file(head, "head")
    _existing_file(public_key, "public key", single_link=False)
    _distinct_files({"ledger": log, "head": head, "public key": public_key})
    log_bytes = _read_regular(log, "ledger")
    head_bytes = _read_regular(head, "head")
    signed = parse_head(head_bytes, f"head {head}")
    records = parse_log(log_bytes, f"ledger {log}")
    if signed["size"] > len(records):
        raise VerificationError(f"head {head} covers {signed['size']} records but ledger {log} has {len(records)}")
    if signed["size"] < len(records):
        raise StaleHead(
            f"head {head} covers {signed['size']} of the {len(records)} records in {log}; "
            "sign a current head before reading"
        )
    with tempfile.TemporaryDirectory(prefix="eb-ledger-verify-") as private:
        copy_log = os.path.join(private, "ledger.jsonl")
        copy_head = os.path.join(private, "head.json")
        _write_private(copy_log, log_bytes)
        _write_private(copy_head, head_bytes)
        _Verifier(eb_stack, public_key, timeout).binds(copy_log, copy_head, signed, records)
    return VerifiedLog(signed, records)


# Observed-history backfill


def _text(value, what, path=False):
    if not _non_empty(value) or value != value.strip() or _CONTROL.search(value):
        raise Refused(f"manifest {what} must be a non-empty string without control characters or edge spaces")
    if path and (not os.path.isabs(value) or os.path.normpath(value) != value):
        raise Refused(f"manifest {what} must be an absolute, normalised path")
    return value


def _only(mapping, allowed, what):
    if not isinstance(mapping, dict):
        raise Refused(f"manifest {what} must be a JSON object")
    extra = sorted(set(mapping) - set(allowed))
    if extra:
        raise Refused(f"manifest {what} has unknown fields {', '.join(extra)}")


def load_manifest(path, expected_sha256, now):
    """An observed-history manifest, refused unless its bytes hash to ``expected_sha256``."""
    _existing_file(path, "backfill manifest")
    if not isinstance(expected_sha256, str) or not _HEX64.match(expected_sha256):
        raise Refused("the manifest SHA-256 must be 64 lower-case hex characters")
    data = _read_regular(path, "backfill manifest")
    digest = _sha256(data)
    if digest != expected_sha256:
        raise Refused(f"manifest {path} hashes to {digest}, not the pinned {expected_sha256}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Refused(f"manifest {path} is not UTF-8") from exc
    manifest = _strict_json(text, f"manifest {path}")
    _only(manifest, ("schema", "manifest_id", "observed_at", "observer", "stack", "target", "observations"), "")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise Refused(f"manifest schema must be {MANIFEST_SCHEMA}")
    if not isinstance(manifest.get("manifest_id"), str) or not _MANIFEST_ID.match(manifest["manifest_id"]):
        raise Refused("manifest manifest_id must be 1-64 of A-Z a-z 0-9 . _ - starting alphanumeric")
    observed = manifest.get("observed_at")
    if not isinstance(observed, str) or not _OBSERVED_TS.match(observed):
        raise Refused("manifest observed_at must be YYYY-MM-DDTHH:MM:SSZ")
    try:
        observed_seconds = calendar.timegm(time.strptime(observed, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError as exc:
        raise Refused(f"manifest observed_at {observed} is not a time") from exc
    if observed_seconds > now:
        raise Refused(f"manifest observed_at {observed} is after the current time {_utc(now)}")
    for key in ("observer", "stack", "target"):
        _text(manifest.get(key), key)
    observations = manifest.get("observations")
    if not isinstance(observations, list) or not observations:
        raise Refused("manifest observations must be a non-empty list")
    seen = set()
    for number, item in enumerate(observations):
        where = f"observations[{number}]"
        _only(item, ("module", "location", "modulefile", "provenance", "note"), where)
        _text(item.get("module"), f"{where}.module")
        _text(item.get("location"), f"{where}.location", path=True)
        if (item["module"], item["location"]) in seen:
            raise Refused(f"manifest {where} repeats {item['module']} at {item['location']}")
        seen.add((item["module"], item["location"]))
        if "note" in item:
            _text(item["note"], f"{where}.note")
        if "modulefile" in item:
            modulefile = item["modulefile"]
            _only(modulefile, ("path", "sha256"), f"{where}.modulefile")
            _text(modulefile.get("path"), f"{where}.modulefile.path", path=True)
            if not isinstance(modulefile.get("sha256"), str) or not _HEX64.match(modulefile["sha256"]):
                raise Refused(f"manifest {where}.modulefile.sha256 must be 64 lower-case hex characters")
        provenance = item.get("provenance")
        if not isinstance(provenance, list) or not provenance:
            raise Refused(f"manifest {where}.provenance must name at least one original reference")
        for ref_number, ref in enumerate(provenance):
            ref_where = f"{where}.provenance[{ref_number}]"
            _only(ref, ("ref", "kind", "sha256"), ref_where)
            _text(ref.get("ref"), f"{ref_where}.ref")
            if "kind" in ref:
                _text(ref["kind"], f"{ref_where}.kind")
            if "sha256" in ref and (not isinstance(ref["sha256"], str) or not _HEX64.match(ref["sha256"])):
                raise Refused(f"manifest {ref_where}.sha256 must be 64 lower-case hex characters")
    return manifest, digest


def backfill_records(manifest, digest):
    """One ``finding`` record per observation, stamped with the observation time.

    Nothing claims an install or a build happened: the record says the
    software was seen at ``observed_at`` and names where the evidence is.
    """
    records = []
    for item in manifest["observations"]:
        place = _sha256(item["location"].encode("utf-8"))[:16]
        build = {
            "event_id": f"eb-staged-rebuild/backfill/{manifest['manifest_id']}/{item['module']}@{place}",
            "tool": TOOL,
            "kind": BACKFILL_KIND,
            "manifest_id": manifest["manifest_id"],
            "manifest_sha256": digest,
            "observed_at": manifest["observed_at"],
            "provenance": item["provenance"],
        }
        if "note" in item:
            build["note"] = item["note"]
        record = {
            "ts": manifest["observed_at"],
            "actor": manifest["observer"],
            "stack": manifest["stack"],
            "target": manifest["target"],
            "module": item["module"],
            "location": item["location"],
            "action": "finding",
            "reason": (
                f"observed by backfill manifest {manifest['manifest_id']} at {manifest['observed_at']}; "
                "no history before the observation is recorded"
            ),
            "build": build,
        }
        if "modulefile" in item:
            record["modulefile"] = dict(item["modulefile"])
        records.append(record)
    return records


def _body(record):
    return {key: value for key, value in record.items() if key not in ("id", "index", "sig")}


def _check_receipt(receipt, event_id):
    valid = (
        isinstance(receipt, dict)
        and isinstance(receipt.get("index"), int) and not isinstance(receipt["index"], bool)
        and isinstance(receipt.get("head_size"), int) and not isinstance(receipt["head_size"], bool)
        and 0 <= receipt["index"] < receipt["head_size"]
        and isinstance(receipt.get("id"), str) and _HEX64.match(receipt["id"])
        and isinstance(receipt.get("head_root"), str) and _HEX64.match(receipt["head_root"])
        and isinstance(receipt.get("replayed"), bool)
        and receipt.get("verified") is True
    )
    if not valid:
        raise PublisherError(f"malformed ledger receipt for {event_id}: {receipt!r}")
    return receipt


def backfill(eb_stack, log, key, head, public_key, manifest_path, manifest_sha256,
             timeout=DEFAULT_TIMEOUT, faults=None, clock=time.time):
    """Append the manifest's observations, each at most once.

    Every record is built from the manifest alone, so a retry sends the same
    bytes and ``eb-stack`` acknowledges it as already recorded. The log is
    checked first: a manifest id already backfilled from other bytes, or an
    event id recorded with other content, is refused before anything is
    appended. Each receipt needs ``eb-stack ledger verify`` to prove the
    record is in the signed head written by the append.
    """
    faults = faults or FaultInjector()
    _executable(eb_stack)
    _new_or_file(log, "ledger")
    _new_or_file(head, "head")
    _existing_file(key, "signing key")
    _existing_file(public_key, "public key", single_link=False)
    _distinct_files({"ledger": log, "head": head, "signing key": key, "public key": public_key,
                     "backfill manifest": manifest_path})
    _key_outside(key, {"ledger directory": os.path.dirname(log), "head directory": os.path.dirname(head)})
    manifest, digest = load_manifest(manifest_path, manifest_sha256, clock())
    records = backfill_records(manifest, digest)
    # eb-stack quarantines an interrupted tail on the next append.
    existing = parse_log(_committed_prefix(_read_regular(log, "ledger")), f"ledger {log}") if os.path.lexists(log) else []
    if existing and existing[0]["stack"] != manifest["stack"]:
        raise Refused(f"manifest stack {manifest['stack']!r} is not the ledger's stack {existing[0]['stack']!r}")
    by_event = {}
    for record in existing:
        build = record.get("build")
        if not isinstance(build, dict):
            continue
        if (build.get("kind") == BACKFILL_KIND and build.get("manifest_id") == manifest["manifest_id"]
                and build.get("manifest_sha256") != digest):
            raise Refused(
                f"manifest {manifest['manifest_id']} was backfilled from bytes hashing to "
                f"{build.get('manifest_sha256')}, not {digest}; a backfill manifest is immutable"
            )
        if "event_id" in build:
            by_event[build["event_id"]] = record
    for record in records:
        event_id = record["build"]["event_id"]
        if event_id in by_event and _body(by_event[event_id]) != record:
            raise Refused(f"ledger record {by_event[event_id]['index']} holds {event_id} with other content")
    ledger = EbStackLedger({
        "eb_stack": eb_stack, "log": log, "key": key, "head": head,
        "public_key": public_key, "timeout": timeout, "required": True,
    })
    receipts = []
    with tempfile.TemporaryDirectory(prefix="eb-ledger-backfill-") as private:
        for number, record in enumerate(records):
            event_id = record["build"]["event_id"]
            path = os.path.join(private, f"{number:06d}.json")
            _write_private(path, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
            faults.reach(f"backfill.append:{event_id}")
            receipt = _check_receipt(ledger.append(path), event_id)
            receipts.append(dict(receipt, event_id=event_id))
    return {
        "manifest_id": manifest["manifest_id"],
        "manifest_sha256": digest,
        "appended": sum(not receipt["replayed"] for receipt in receipts),
        "replayed": sum(receipt["replayed"] for receipt in receipts),
        "receipts": receipts,
    }


# Daily anchors


def _anchor_path(anchors, day):
    return os.path.join(anchors, f"{day}.head.json")


def list_anchors(anchors):
    """Anchors by day, each a single-link regular file whose head is signed that day."""
    found = []
    for name in sorted(os.listdir(anchors)):
        match = _ANCHOR_NAME.match(name)
        if match is None:
            continue
        path = os.path.join(anchors, name)
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise Refused(f"anchor {path} must be a regular file with one link")
        data = _read_regular(path, "anchor")
        head = parse_head(data, f"anchor {path}")
        if head["ts"][:10] != match.group(1):
            raise Refused(f"anchor {path} holds a head signed at {head['ts']}, not on {match.group(1)}")
        found.append({"day": match.group(1), "path": path, "head": head, "bytes": data})
    for older, newer in itertools.pairwise(found):
        a, b = older["head"], newer["head"]
        if a["public_key"] != b["public_key"]:
            raise Equivocation(f"anchors {older['path']} and {newer['path']} are signed by different keys")
        if b["ts"] < a["ts"]:
            raise Refused(f"anchor {newer['path']} is signed before {older['path']}")
        if b["size"] < a["size"] or (b["size"] == a["size"] and b["root"] != a["root"]):
            raise Equivocation(f"anchor {newer['path']} does not extend {older['path']}")
    return found


def _clear_staging(anchors):
    for name in os.listdir(anchors):
        if name.startswith(_STAGING_PREFIX):
            path = os.path.join(anchors, name)
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)
    fsync_dir(anchors)


def _anchor_paths(eb_stack, log, key, public_key, anchors):
    _executable(eb_stack)
    _existing_file(log, "ledger")
    _existing_file(public_key, "public key", single_link=False)
    _existing_dir(anchors, "anchor directory")
    if key is not None:
        _existing_file(key, "signing key")
        _key_outside(key, {"ledger directory": os.path.dirname(log), "anchor directory": anchors})
    if anchors == os.path.dirname(log) or _inside(log, anchors):
        raise Refused(f"anchor directory {anchors} must not hold the ledger {log}")
    _distinct_files({"ledger": log, "signing key": key, "public key": public_key})


def _pin(private, name, data):
    path = os.path.join(private, name)
    _write_private(path, data)
    return path


def anchor(eb_stack, log, key, public_key, anchors, timeout=DEFAULT_TIMEOUT, faults=None,
           clock=time.time, lock_timeout=60):
    """Publish today's signed head as ``<anchors>/<YYYY-MM-DD>.head.json``, once.

    The head is signed now, for the day it is signed on: no anchor is made
    for another day. It must bind to the log and extend the latest anchor,
    both proved by ``eb-stack ledger verify``, before it is linked into
    place. An existing file is never replaced: when today's anchor exists
    the call proves it still extends into the log and reports it.
    """
    faults = faults or FaultInjector()
    _anchor_paths(eb_stack, log, key, public_key, anchors)
    lock = FileLock(anchors)
    lock.acquire(timeout=lock_timeout)
    try:
        _clear_staging(anchors)
        ts = _utc(clock())
        day = ts[:10]
        existing = list_anchors(anchors)
        later = [item for item in existing if item["day"] > day]
        if later:
            raise Refused(f"anchor {later[-1]['path']} is for a day after {day}; check the clock")
        today = next((item for item in existing if item["day"] == day), None)
        prior = today or (existing[-1] if existing else None)
        staging = tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=anchors)
        try:
            candidate = os.path.join(staging, "head.json")
            faults.reach("anchor.sign")
            code, stdout, stderr = _bounded(
                [eb_stack, "ledger", "head", "--log", log, "--key", key, "--out", candidate, "--ts", ts], timeout
            )
            if code != 0:
                raise PublisherError(f"eb-stack ledger head exited {code}: {stderr.strip()[-500:]}")
            signed_bytes = _read_regular(candidate, "signed head")
            signed = parse_head(signed_bytes, "the signed head")
            lines = stdout.splitlines(keepends=True)
            receipt = f"signed head size {signed['size']} root {signed['root']} at {ts} to {candidate}\n"
            checkpoint = _CHECKPOINT.match(lines[1].rstrip("\n")) if len(lines) == 2 else None
            if lines[:1] != [receipt] or checkpoint is None or checkpoint.groups() != (str(signed["size"]), signed["root"]):
                raise PublisherError(f"eb-stack ledger head printed an unexpected receipt: {stdout[-500:]!r}")
            if signed["ts"] != ts:
                raise PublisherError(f"eb-stack ledger head signed at {signed['ts']}, not {ts}")
            snapshot = _committed_prefix(_read_regular(log, "ledger"))
            records = parse_log(snapshot, f"ledger {log}")
            verifier = _Verifier(eb_stack, public_key, timeout)
            private_log = _pin(staging, "ledger.jsonl", snapshot)
            verifier.binds(private_log, candidate, signed, records)
            if prior is not None:
                prior_path = _pin(staging, "prior.json", prior["bytes"])
                verifier.extends(private_log, prior_path, prior["head"], candidate, signed)
            if today is not None:
                return {"status": "exists", "day": day, "path": today["path"],
                        "size": today["head"]["size"], "root": today["head"]["root"]}
            target = _anchor_path(anchors, day)
            os.chmod(candidate, 0o444)
            fd = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            faults.reach("anchor.publish")
            try:
                os.link(candidate, target, follow_symlinks=False)
            except FileExistsError as exc:
                raise Refused(f"anchor {target} appeared while this one was prepared; rerun to verify it") from exc
            fsync_dir(anchors)
            faults.reach("anchor.published")
            return {"status": "published", "day": day, "path": target,
                    "size": signed["size"], "root": signed["root"]}
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            fsync_dir(anchors)
    finally:
        lock.release()


def verify_anchors(eb_stack, log, public_key, anchors, timeout=DEFAULT_TIMEOUT):
    """Prove every anchor extends the one before it and the latest binds to the log."""
    _anchor_paths(eb_stack, log, None, public_key, anchors)
    found = list_anchors(anchors)
    if not found:
        return {"anchors": 0}
    snapshot = _committed_prefix(_read_regular(log, "ledger"))
    records = parse_log(snapshot, f"ledger {log}")
    verifier = _Verifier(eb_stack, public_key, timeout)
    with tempfile.TemporaryDirectory(prefix="eb-ledger-anchors-") as private:
        private_log = _pin(private, "ledger.jsonl", snapshot)
        pinned = [_pin(private, f"{number:06d}.json", item["bytes"]) for number, item in enumerate(found)]
        for number in range(1, len(found)):
            verifier.extends(private_log, pinned[number - 1], found[number - 1]["head"],
                             pinned[number], found[number]["head"])
        verifier.binds(private_log, pinned[-1], found[-1]["head"], records)
    latest = found[-1]
    return {"anchors": len(found), "latest": latest["path"], "size": latest["head"]["size"],
            "root": latest["head"]["root"]}


# Package history projections


def package_of(module):
    return module.split("/", 1)[0]


def _effective(records):
    """Which records a later applied correction voided, and why.

    A correction applies when it is stageless or ``finished``, names an
    earlier record, and is not itself voided. Walking from the newest record
    settles corrections of corrections.
    """
    by_id = {record["id"]: record["index"] for record in records}
    voided = set()
    corrected_by = {}
    pending_by = {}
    problems = []
    for record in records:
        for field in _LINKS:
            if field in record and record[field] not in by_id:
                problems.append({"index": record["index"], "field": field, "target": record[field],
                                 "problem": "names no record in this log"})
    for record in reversed(records):
        if record["action"] != "correction":
            continue
        target = by_id.get(record["corrects"])
        if target is None:
            continue
        if target >= record["index"]:
            problems.append({"index": record["index"], "field": "corrects", "target": record["corrects"],
                             "problem": "names a record that is not earlier"})
            continue
        stage = record.get("stage")
        if record["index"] in voided or stage == "aborted":
            continue
        if stage == "started":
            pending_by.setdefault(target, []).append(record["index"])
            continue
        voided.add(target)
        corrected_by.setdefault(target, []).append(record["index"])
    return voided, corrected_by, pending_by, problems


_STATE = {
    "install": "installed", "rebuild-inplace": "installed", "rebuild-bump": "installed",
    "unretire": "installed", "retire": "retired", "remove": "removed", "move": "moved",
}


def build_history(verified):
    """The deterministic history model of a verified log, grouped by package."""
    records = verified.records
    voided, corrected_by, pending_by, problems = _effective(records)
    by_id = {record["id"]: record["index"] for record in records}
    packages = {}
    for record in records:
        index = record["index"]
        build = record.get("build") if isinstance(record.get("build"), dict) else {}
        observed = build.get("kind") == BACKFILL_KIND
        event = {
            "index": index,
            "id": record["id"],
            "ts": record["ts"],
            "action": record["action"],
            "stage": record.get("stage"),
            "actor": record["actor"],
            "target": record["target"],
            "module": record["module"],
            "location": record["location"],
            "event_id": build.get("event_id"),
            "reason": record.get("reason"),
            "modulefile": record.get("modulefile"),
            "links": {field: {"id": record[field], "index": by_id.get(record[field])}
                      for field in _LINKS if field in record},
            "observed": observed,
            "provenance": build.get("provenance") if observed else None,
            "manifest_id": build.get("manifest_id") if observed else None,
            "status": "corrected" if index in voided else "effective",
            "corrected_by": corrected_by.get(index, []),
            "correction_pending_by": pending_by.get(index, []),
        }
        packages.setdefault(package_of(record["module"]), []).append(event)
    model = []
    for name in sorted(packages):
        events = packages[name]
        modules = {}
        for event in events:
            state = modules.setdefault(event["module"], {
                "module": event["module"], "state": "unknown", "location": None,
                "last_change": None, "access": [], "findings": [],
            })
            if event["status"] != "effective" or event["stage"] not in (None, "finished"):
                continue
            action = event["action"]
            if action == "access":
                state["access"].append(event["index"])
            elif action == "finding":
                state["findings"].append(event["index"])
                if state["state"] == "unknown":
                    state.update(state="observed", location=event["location"], last_change=event["index"])
            elif action in _STATE:
                state.update(state=_STATE[action], location=event["location"], last_change=event["index"])
        package_problems = [problem for problem in problems
                            if package_of(records[problem["index"]]["module"]) == name]
        model.append({"name": name, "modules": [modules[key] for key in sorted(modules)],
                      "events": events, "problems": package_problems})
    head = verified.head
    return {
        "schema": HISTORY_SCHEMA,
        "head": {key: head[key] for key in ("size", "root", "ts", "public_key")},
        "stack": records[0]["stack"] if records else None,
        "packages": model,
        "problems": problems,
    }


def file_stem(name):
    """A file name for a package that cannot leave the output directory."""
    stem = urllib.parse.quote(name, safe="")
    if stem in ("", ".", "..") or len(stem) > 150:
        stem = stem[:100] + "-" + _sha256(name.encode("utf-8"))[:16]
    return stem


def md(value):
    """Text safe in a Markdown table cell: no markup, HTML, line breaks or bidi controls."""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = _canonical(value)
    out = []
    for char in value:
        if char == "&":
            out.append("&amp;")
        elif char == "<":
            out.append("&lt;")
        elif char == ">":
            out.append("&gt;")
        elif _CONTROL.match(char):
            out.append(f"U+{ord(char):04X}")
        elif char in "\\`*_[]()|~!#":
            out.append("\\" + char)
        else:
            out.append(char)
    return "".join(out)


def _details(event):
    parts = []
    for field, link in sorted(event["links"].items()):
        where = f"#{link['index']}" if link["index"] is not None else f"unknown record {link['id']}"
        parts.append(f"{field} {where}")
    if event["corrected_by"]:
        parts.append("corrected by " + ", ".join(f"#{index}" for index in event["corrected_by"]))
    if event["correction_pending_by"]:
        parts.append("correction started by " + ", ".join(f"#{index}" for index in event["correction_pending_by"]))
    if event["observed"]:
        refs = "; ".join(ref["ref"] + (f" sha256 {ref['sha256']}" if "sha256" in ref else "")
                         for ref in event["provenance"])
        parts.append(f"observed by manifest {event['manifest_id']}; provenance {refs}")
    if event["reason"] is not None:
        parts.append(f"reason {event['reason'] if isinstance(event['reason'], str) else _canonical(event['reason'])}")
    return md("; ".join(parts))


def _head_lines(model):
    head = model["head"]
    return [
        f"Stack: {md(model['stack'])}",
        "",
        f"Verified head: size {head['size']}, root {head['root']}, signed {head['ts']} by {head['public_key']}.",
        "",
    ]


def render_package(model, package):
    lines = [f"# Package history: {md(package['name'])}", ""] + _head_lines(model)
    lines += ["## Modules", "", "| Module | State | Last change | Location |", "| --- | --- | --- | --- |"]
    for module in package["modules"]:
        change = f"#{module['last_change']}" if module["last_change"] is not None else ""
        lines.append(f"| {md(module['module'])} | {module['state']} | {change} | {md(module['location'])} |")
    lines += ["", "## Events", "",
              "| # | Time (UTC) | Action | Stage | Module | Actor | Status | Details |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for event in package["events"]:
        lines.append(
            f"| {event['index']} | {md(event['ts'])} | {event['action']} | {event['stage'] or ''} | "
            f"{md(event['module'])} | {md(event['actor'])} | {event['status']} | {_details(event)} |"
        )
    if package["problems"]:
        lines += ["", "## Problems", ""]
        for problem in package["problems"]:
            lines.append(f"* record #{problem['index']} {problem['field']} {md(problem['target'])}: {problem['problem']}")
    return "\n".join(lines) + "\n"


def render(model):
    """Every output file, by path relative to the output directory."""
    files = {}
    index = []
    stems = set()
    for package in model["packages"]:
        stem = file_stem(package["name"])
        if stem in stems:
            raise Refused(f"two packages map to the file name {stem}")
        stems.add(stem)
        document = {"schema": model["schema"], "head": model["head"], "stack": model["stack"],
                    "package": package["name"], "modules": package["modules"],
                    "events": package["events"], "problems": package["problems"]}
        files[f"packages/{stem}.json"] = _json_bytes(document)
        files[f"packages/{stem}.md"] = render_package(model, package).encode("utf-8")
        index.append({"name": package["name"], "json": f"packages/{stem}.json", "markdown": f"packages/{stem}.md",
                      "modules": len(package["modules"]), "events": len(package["events"])})
    files["index.json"] = _json_bytes({"schema": model["schema"], "head": model["head"], "stack": model["stack"],
                                       "packages": index, "problems": model["problems"]})
    lines = ["# Ledger package history", ""] + _head_lines(model)
    lines += ["| Package | Modules | Events |", "| --- | --- | --- |"]
    for entry in index:
        link = urllib.parse.quote(entry["markdown"])
        lines.append(f"| [{md(entry['name'])}]({link}) | {entry['modules']} | {entry['events']} |")
    files["index.md"] = ("\n".join(lines) + "\n").encode("utf-8")
    return files


def _json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("ascii")


def _output_dir(out, log, head, public_key):
    _plain_path(out, "output directory")
    for what, path in (("ledger", log), ("head", head), ("public key", public_key)):
        if _inside(path, out) or out == os.path.dirname(path):
            raise Refused(f"output directory {out} must not hold the {what} {path}")
    if not os.path.lexists(out):
        return
    _existing_dir(out, "output directory")
    names = set(os.listdir(out))
    foreign = names - {"index.json", "index.md", ".index.json.new", ".index.md.new", "packages"}
    if "packages" in names:
        packages = os.path.join(out, "packages")
        if os.path.islink(packages) or not os.path.isdir(packages):
            raise Refused(f"{packages} must be a directory")
        foreign |= {name for name in os.listdir(packages)
                    if not name.endswith((".json", ".md", ".json.new", ".md.new"))}
    if foreign:
        raise Refused(f"output directory {out} holds files that are not a ledger history projection: "
                      f"{', '.join(sorted(foreign))}")
    if "index.json" in names:
        try:
            schema = json.loads(_read_regular(os.path.join(out, "index.json"), "projection index")).get("schema")
        except (ValueError, AttributeError):
            schema = None
        if schema != HISTORY_SCHEMA:
            raise Refused(f"output directory {out} holds an index that is not a ledger history projection")


def project(eb_stack, log, head, public_key, out, timeout=DEFAULT_TIMEOUT, faults=None):
    """Write JSON and Markdown history of every package in a verified log.

    The same verified log and head always give the same bytes. Package files
    are replaced one at a time and the index last, so an interrupted run
    leaves the previous index; files of packages no longer listed are removed.
    """
    faults = faults or FaultInjector()
    _output_dir(out, log, head, public_key)
    verified = read_verified(eb_stack, log, head, public_key, timeout)
    files = render(build_history(verified))
    packages = os.path.join(out, "packages")
    os.makedirs(packages, exist_ok=True)
    if os.path.islink(packages) or not os.path.isdir(packages):
        raise Refused(f"{packages} must be a directory")
    for relative in sorted(files):
        if relative.startswith("packages/"):
            atomic_write(os.path.join(out, relative), files[relative], faults, f"projection.{relative}")
    keep = {relative.split("/", 1)[1] for relative in files if relative.startswith("packages/")}
    for name in sorted(os.listdir(packages)):
        if name not in keep and name.endswith((".json", ".md")):
            os.unlink(os.path.join(packages, name))
    fsync_dir(packages)
    for relative in ("index.md", "index.json"):
        atomic_write(os.path.join(out, relative), files[relative], faults, f"projection.{relative}")
    return {"out": out, "size": verified.head["size"], "root": verified.head["root"],
            "packages": len(keep) // 2, "files": sorted(files)}


# Command line


def _parser():
    parser = argparse.ArgumentParser(prog="python -m eb_staged_rebuild.ledger_maintenance",
                                     description="Maintain and read an eb-stack ledger.")
    commands = parser.add_subparsers(dest="command", required=True)

    def common(command, key=False, head=False):
        command.add_argument("--eb-stack", required=True, help="eb-stack executable (absolute path)")
        command.add_argument("--log", required=True, help="ledger JSON Lines log (absolute path)")
        command.add_argument("--public-key", required=True, help="Ed25519 public key heads are signed by")
        if key:
            command.add_argument("--key", required=True, help="Ed25519 PKCS#8 PEM signing key, off the log's tree")
        if head:
            command.add_argument("--head", required=True, help="signed head file")
        command.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds per eb-stack call")

    command = commands.add_parser("backfill", help="append observed history from a pinned manifest")
    common(command, key=True, head=True)
    command.add_argument("--manifest", required=True)
    command.add_argument("--manifest-sha256", required=True)
    command = commands.add_parser("anchor", help="publish today's immutable signed anchor")
    common(command, key=True)
    command.add_argument("--anchors", required=True)
    command = commands.add_parser("verify-anchors", help="prove the anchor chain against the log")
    common(command)
    command.add_argument("--anchors", required=True)
    command = commands.add_parser("project", help="write package history from a verified log")
    common(command, head=True)
    command.add_argument("--out", required=True)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.timeout <= 0:
        print("ledger-maintenance: --timeout must be positive", file=sys.stderr)
        return 2
    try:
        if args.command == "backfill":
            result = backfill(args.eb_stack, args.log, args.key, args.head, args.public_key,
                              args.manifest, args.manifest_sha256, args.timeout)
        elif args.command == "anchor":
            result = anchor(args.eb_stack, args.log, args.key, args.public_key, args.anchors, args.timeout)
        elif args.command == "verify-anchors":
            result = verify_anchors(args.eb_stack, args.log, args.public_key, args.anchors, args.timeout)
        else:
            result = project(args.eb_stack, args.log, args.head, args.public_key, args.out, args.timeout)
    except PublisherError as exc:
        print(f"ledger-maintenance: {exc}; nothing was acknowledged, retry the same command", file=sys.stderr)
        return PENDING_EXIT
    except (Refused, OSError) as exc:
        print(f"ledger-maintenance: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

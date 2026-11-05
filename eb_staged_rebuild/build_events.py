"""Opt-in ledger events around one software-build command.

A caller names one operation with explicit metadata and a ledger
configuration, then hands over the command. The ``started`` record must be
acknowledged by ``eb-stack ledger append`` before the command runs; with
``required`` false the command runs anyway and the record stays pending.
After the command a ``finished`` record (exit status 0 and every post-build
check passed) or an ``aborted`` record (anything else) follows.

Records are queued durably in ``<outbox>/<operation>/outbox.json`` before
delivery and are delivered in order through the bounded adapter in
``ledger_adapter``. Each record keeps its bytes and its ``build.event_id``
until the ledger returns an exact receipt, so a retry after a lost
acknowledgement is acknowledged once by eb-stack. A retry never repeats a
command that already ran: its recorded result is returned instead.

Only the allowlisted metadata below is serialized. The environment, the
command and its arguments are never written to the outbox or the ledger.

Use ``run_build`` and ``deliver`` from the main thread; the command runs in
its own process group, which is stopped before either returns.

Command line::

    python3 -m eb_staged_rebuild.build_events --config C.json --metadata M.json -- CMD ARG...
    python3 -m eb_staged_rebuild.build_events --config C.json --metadata M.json --deliver
"""

import argparse
import hashlib
import json
import os
import re
import signal
import stat
import sys
import threading
import time
import urllib.parse
from collections import namedtuple

from eb_staged_rebuild import outbox
from eb_staged_rebuild.atomicio import atomic_write, read_bytes
from eb_staged_rebuild.builders import run_command
from eb_staged_rebuild.errors import NeedsIntervention, Refused
from eb_staged_rebuild.faults import FaultInjector
from eb_staged_rebuild.ledger_adapter import EbStackLedger, _utc
from eb_staged_rebuild.lockfile import FileLock

SCHEMA = 1
PRODUCER = "eb-staged-rebuild/build_events"
PENDING_EXIT = outbox.PENDING_EXIT
REFUSED_EXIT = 2
POSTCHECK_EXIT = 65
UNKNOWN_EXIT = 70
NOT_STARTED_EXIT = 127

_ACTIONS = ("install", "rebuild-inplace", "rebuild-bump")
_CONFIG_PATHS = ("eb_stack", "log", "key", "outbox")
_CONFIG_KEYS = set(_CONFIG_PATHS) | {"public_key", "timeout", "required", "lock_timeout"}
# Compared on retry: a different ledger, key or delivery policy is another operation.
_POLICY_KEYS = ("eb_stack", "log", "key", "public_key", "outbox", "required")
_METADATA_KEYS = {
    "operation", "actor", "stack", "target", "module", "location", "action",
    "easyconfig", "sources", "patches", "modulefile", "tool", "logs", "artifact",
}
_OPERATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ACCESSION = re.compile(r"^sha256:[0-9a-f]{64}$")
_SEGMENTS = re.compile(r"^[A-Za-z0-9_.+-]+(/[A-Za-z0-9_.+-]+)*$")
_FILE_NAME = re.compile(r"^[A-Za-z0-9_.+~-][A-Za-z0-9_.+~=,-]*$")
_TEXT_LIMIT = 1024
_LIST_LIMIT = 256
_CHUNK = 1024 * 1024
_JSON_LIMIT = 1024 * 1024

Outcome = namedtuple("Outcome", "returncode phase command_status pending message")


def _text(value, what, limit=_TEXT_LIMIT):
    if not isinstance(value, str) or not value or len(value) > limit:
        raise Refused(f"{what} must be a non-empty string of at most {limit} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise Refused(f"{what} contains a control character")
    if "-----BEGIN" in value:
        raise Refused(f"{what} looks like key material; keys never go in a record")
    return value


def _clean_path(value, what):
    _text(value, what, 4096)
    if not os.path.isabs(value) or os.path.normpath(value) != value or value.startswith("//"):
        raise Refused(f"{what} must be an absolute, normalized path: {value!r}")
    return value


def _inside(path, directory):
    return path == directory or path.startswith(directory.rstrip(os.sep) + os.sep)


def _object(value, what, allowed):
    if not isinstance(value, dict):
        raise Refused(f"{what} must be a JSON object")
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise Refused(f"{what} has fields outside the build-event schema: {', '.join(unknown)}")
    return value


def _sha(value, what):
    if not isinstance(value, str) or not _SHA256.match(value):
        raise Refused(f"{what} must be 64 lower-case hex characters")
    return value


def _uri(value, what):
    _text(value, what, 2048)
    parts = urllib.parse.urlsplit(value)
    if not parts.scheme or "@" in parts.netloc or parts.query or parts.fragment:
        raise Refused(f"{what} must be a URI without credentials, query or fragment")
    return value


def _read_json(path, what):
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_JSON_LIMIT + 1)
        if len(raw) > _JSON_LIMIT:
            raise ValueError(f"larger than {_JSON_LIMIT} bytes")
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise Refused(f"cannot read {what} {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise Refused(f"{what} {path} is not a JSON object")
    return data


def check_config(config):
    """Normalize a configuration object; refuse unsafe or unknown settings."""
    config = dict(_object(config, "build-event config", _CONFIG_KEYS))
    for key in _CONFIG_PATHS:
        if key not in config:
            raise Refused(f"build-event config needs {key}")
        _clean_path(config[key], f"build-event config {key}")
    if config.get("public_key") is not None:
        _clean_path(config["public_key"], "build-event config public_key")
    else:
        config.pop("public_key", None)
    config.setdefault("required", True)
    if not isinstance(config["required"], bool):
        raise Refused("build-event config required must be true or false")
    for key, default in (("timeout", 120), ("lock_timeout", 0)):
        config.setdefault(key, default)
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or (key == "timeout" and value == 0):
            raise Refused(f"build-event config {key} must be a {'positive' if key == 'timeout' else 'non-negative'} number of seconds")
    box = config["outbox"]
    if os.path.lexists(box) and not os.path.isdir(box):
        raise Refused(f"build-event outbox {box} is not a directory")
    if os.path.realpath(box) != box:
        raise Refused(f"build-event outbox {box} passes through a symbolic link")
    log_dir = os.path.realpath(os.path.dirname(config["log"]))
    if _inside(box, log_dir) or _inside(log_dir, box):
        raise Refused(f"build-event outbox {box} and ledger directory {log_dir} must be disjoint")
    key = config["key"]
    try:
        info = os.lstat(key)
    except OSError as exc:
        raise Refused(f"signing key {key} cannot be inspected: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise Refused(f"signing key {key} is not a regular file")
    for directory, what in ((log_dir, "ledger directory"), (box, "build-event outbox")):
        if _inside(os.path.realpath(key), directory):
            raise Refused(f"signing key {key} is inside the {what} {directory}")
    return config


def load_config(path):
    return check_config(_read_json(path, "build-event config"))


def _file_list(value, what):
    if not isinstance(value, list) or len(value) > _LIST_LIMIT:
        raise Refused(f"{what} must be a list of at most {_LIST_LIMIT} entries")
    items = []
    for at, item in enumerate(value):
        item = _object(item, f"{what}[{at}]", ("name", "sha256"))
        name = _text(item.get("name"), f"{what}[{at}].name", 255)
        if not _FILE_NAME.match(name) or name in (".", ".."):
            raise Refused(f"{what}[{at}].name must be a plain file name: {name!r}")
        items.append({"name": name, "sha256": _sha(item.get("sha256"), f"{what}[{at}].sha256")})
    return items


def check_metadata(metadata):
    """Normalize caller metadata. Fields outside the schema are refused."""
    meta = _object(metadata, "build-event metadata", _METADATA_KEYS)
    out = {}
    operation = _text(meta.get("operation"), "operation", 128)
    if not _OPERATION.match(operation) or ".." in operation:
        raise Refused(f"operation must match {_OPERATION.pattern} without '..': {operation!r}")
    out["operation"] = operation
    for key in ("actor", "stack", "module"):
        out[key] = _text(meta.get(key), key)
    if any(char.isspace() for char in out["module"]):
        raise Refused("module must not contain whitespace")
    out["location"] = _clean_path(meta.get("location"), "location")
    target = _object(meta.get("target"), "target", ("cpu", "accelerator"))
    cpu = _text(target.get("cpu"), "target.cpu", 256)
    if not _SEGMENTS.match(cpu):
        raise Refused(f"target.cpu must be slash-separated names: {cpu!r}")
    out["target"] = {"cpu": cpu}
    if target.get("accelerator") is not None:
        accelerator = _text(target["accelerator"], "target.accelerator", 256)
        if not _SEGMENTS.match(accelerator):
            raise Refused(f"target.accelerator must be slash-separated names: {accelerator!r}")
        out["target"]["accelerator"] = accelerator
    out["action"] = meta.get("action", "install")
    if out["action"] not in _ACTIONS:
        raise Refused(f"action must be one of {', '.join(_ACTIONS)}")
    easyconfig = _object(meta.get("easyconfig"), "easyconfig", ("path", "sha256"))
    out["easyconfig"] = {"path": _clean_path(easyconfig.get("path"), "easyconfig.path")}
    if easyconfig.get("sha256") is not None:
        out["easyconfig"]["sha256"] = _sha(easyconfig["sha256"], "easyconfig.sha256")
    out["sources"] = _file_list(meta.get("sources", []), "sources")
    out["patches"] = _file_list(meta.get("patches", []), "patches")
    if meta.get("modulefile") is not None:
        modulefile = _object(meta["modulefile"], "modulefile", ("path", "sha256"))
        out["modulefile"] = {"path": _clean_path(modulefile.get("path"), "modulefile.path")}
        if modulefile.get("sha256") is not None:
            out["modulefile"]["sha256"] = _sha(modulefile["sha256"], "modulefile.sha256")
    if meta.get("tool") is not None:
        tool = _object(meta["tool"], "tool", ("name", "version", "revision"))
        out["tool"] = {key: _text(tool.get(key), f"tool.{key}", 256) for key in ("name", "version", "revision") if key in tool}
        if "name" not in out["tool"]:
            raise Refused("tool.name is required")
    logs = meta.get("logs", [])
    if not isinstance(logs, list) or len(logs) > _LIST_LIMIT:
        raise Refused(f"logs must be a list of at most {_LIST_LIMIT} references")
    out["logs"] = []
    for at, ref in enumerate(logs):
        ref = _object(ref, f"logs[{at}]", ("uri", "sha256"))
        entry = {"uri": _uri(ref.get("uri"), f"logs[{at}].uri")}
        if ref.get("sha256") is not None:
            entry["sha256"] = _sha(ref["sha256"], f"logs[{at}].sha256")
        out["logs"].append(entry)
    if meta.get("artifact") is not None:
        artifact = _object(meta["artifact"], "artifact", ("accession", "path", "uri"))
        out["artifact"] = {}
        if artifact.get("accession") is not None:
            if not isinstance(artifact["accession"], str) or not _ACCESSION.match(artifact["accession"]):
                raise Refused("artifact.accession must be sha256:<64 lower-case hex>")
            out["artifact"]["accession"] = artifact["accession"]
        if artifact.get("path") is not None:
            out["artifact"]["path"] = _clean_path(artifact["path"], "artifact.path")
        if artifact.get("uri") is not None:
            out["artifact"]["uri"] = _uri(artifact["uri"], "artifact.uri")
        if "accession" not in out["artifact"] and "path" not in out["artifact"]:
            raise Refused("artifact needs an accession or a path to hash")
    return out


def load_metadata(path):
    return check_metadata(_read_json(path, "build-event metadata"))


def _hash_file(path):
    """SHA-256 and size of a regular file, streamed; a symbolic link is refused."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path} is not a regular file")
        digest, size = hashlib.sha256(), 0
        while True:
            chunk = os.read(fd, _CHUNK)
            if not chunk:
                return digest.hexdigest(), size
            digest.update(chunk)
            size += len(chunk)
    finally:
        os.close(fd)


def _resolve_inputs(meta):
    """Hash the easyconfig when its hash is absent, and check it when present."""
    easyconfig = meta["easyconfig"]
    if os.path.lexists(easyconfig["path"]) or "sha256" not in easyconfig:
        try:
            digest, _size = _hash_file(easyconfig["path"])
        except OSError as exc:
            raise Refused(f"easyconfig {easyconfig['path']} cannot be hashed: {exc}") from exc
        if easyconfig.setdefault("sha256", digest) != digest:
            raise Refused(f"easyconfig {easyconfig['path']} hashes to {digest}, not the supplied {easyconfig['sha256']}")
    return meta


def event_id(operation, stage):
    return f"eb-build-events/{operation}/{stage}"


def target_of(meta):
    target = meta["target"]
    if "accelerator" in target:
        return f"{target['cpu']}/accel/{target['accelerator']}"
    return target["cpu"]


def _record(meta, stage, now):
    build = {
        "event_id": event_id(meta["operation"], stage),
        "operation": meta["operation"],
        "producer": PRODUCER,
        "cpu_target": meta["target"]["cpu"],
    }
    if "accelerator" in meta["target"]:
        build["accelerator"] = meta["target"]["accelerator"]
    if "tool" in meta:
        build["tool"] = dict(meta["tool"])
    if meta["logs"]:
        build["logs"] = [dict(ref) for ref in meta["logs"]]
    record = {
        "ts": _utc(now),
        "actor": meta["actor"],
        "stack": meta["stack"],
        "target": target_of(meta),
        "module": meta["module"],
        "location": meta["location"],
        "action": meta["action"],
        "stage": stage,
        "easyconfig": dict(meta["easyconfig"]),
        "build": build,
    }
    if meta["sources"] or meta["patches"]:
        record["sources"] = {"files": [dict(item) for item in meta["sources"]], "patches": [dict(item) for item in meta["patches"]]}
    return record


def started_record(meta, now, previous_modulefile=None):
    record = _record(meta, "started", now)
    if "modulefile" in meta:
        record["modulefile"] = {"path": meta["modulefile"]["path"]}
        if previous_modulefile:
            record["modulefile"]["previous_sha256"] = previous_modulefile
    return record


def _post_build(meta):
    """Hash the module file and artifact the command produced. Returns (fields, problem)."""
    fields = {}
    if "modulefile" in meta:
        path = meta["modulefile"]["path"]
        try:
            digest, _size = _hash_file(path)
        except OSError as exc:
            return fields, ("modulefile-unreadable", f"module file {path} cannot be hashed: {exc}")
        supplied = meta["modulefile"].get("sha256")
        if supplied is not None and supplied != digest:
            return fields, ("modulefile-mismatch", f"module file {path} hashes to {digest}, not the supplied {supplied}")
        fields["modulefile"] = {"path": path, "sha256": digest}
    if "artifact" in meta:
        artifact = dict(meta["artifact"])
        if "path" in artifact:
            try:
                digest, size = _hash_file(artifact["path"])
            except OSError as exc:
                return fields, ("artifact-unreadable", f"artifact {artifact['path']} cannot be hashed: {exc}")
            accession = f"sha256:{digest}"
            if artifact.setdefault("accession", accession) != accession:
                return fields, ("artifact-mismatch", f"artifact {artifact['path']} is {accession}, not the supplied {artifact['accession']}")
            artifact["size"] = size
        fields["artifact"] = artifact
    return fields, None


def terminal_record(meta, now, status, started_id, previous_modulefile=None):
    """``finished`` for a zero exit whose outputs check, ``aborted`` otherwise."""
    outcome, problem, fields = status["outcome"], None, {}
    if outcome == "exited" and status["exit_status"] == 0:
        fields, problem = _post_build(meta)
        if problem is not None:
            outcome = problem[0]
    stage = "finished" if outcome == "exited" and status["exit_status"] == 0 else "aborted"
    record = _record(meta, stage, now)
    record["build"]["outcome"] = outcome
    if status.get("exit_status") is not None:
        record["build"]["exit_status"] = status["exit_status"]
    if started_id is not None:
        record["build"]["started_id"] = started_id
    if "artifact" in fields:
        record["build"]["artifact"] = fields["artifact"]
    if "modulefile" in fields:
        record["modulefile"] = fields["modulefile"]
    elif "modulefile" in meta:
        record["modulefile"] = {"path": meta["modulefile"]["path"]}
    if previous_modulefile and "modulefile" in record:
        record["modulefile"]["previous_sha256"] = previous_modulefile
    if stage == "aborted":
        record["reason"] = problem[1] if problem else _reason(status)
    return record, (problem[1] if problem else None)


def _reason(status):
    if status["outcome"] == "unknown":
        return "the producer stopped while the command ran; the command's result is unknown"
    if status["outcome"] == "not-started":
        return "the command could not be started"
    return f"the command exited {status['exit_status']}"


def _exit_code(status, problem):
    if status["outcome"] == "unknown":
        return UNKNOWN_EXIT
    if status["outcome"] == "not-started":
        return NOT_STARTED_EXIT
    if status["exit_status"] != 0:
        return status["exit_status"]
    return POSTCHECK_EXIT if problem else 0


class _Operation:
    """The durable state of one operation: ``operation.json`` beside the outbox."""

    def __init__(self, config, meta, faults):
        self.config, self.meta, self.faults = config, meta, faults
        self.dir = os.path.join(config["outbox"], meta["operation"])
        self.path = os.path.join(self.dir, "operation.json")
        ledger = {key: config[key] for key in ("eb_stack", "log", "key", "timeout", "required")}
        if "public_key" in config:
            ledger["public_key"] = config["public_key"]
        ledger["head"] = os.path.join(self.dir, "head.json")
        self.publisher = EbStackLedger(ledger)
        self.policy = {key: config[key] for key in _POLICY_KEYS if key in config}

    def load(self):
        if not os.path.lexists(self.path):
            return None
        try:
            state = json.loads(read_bytes(self.path).decode("utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise NeedsIntervention(f"operation state cannot be read: {self.path}: {exc}") from exc
        if state.get("schema") != SCHEMA:
            raise NeedsIntervention(f"operation state schema {state.get('schema')!r} is not {SCHEMA}")
        return state

    def save(self, state, step):
        raw = json.dumps(state, indent=2, sort_keys=True) + "\n"
        atomic_write(self.path, raw, self.faults, f"build_events.{step}")

    def check(self, state):
        """Refuse a retry whose policy or metadata differ from the recorded operation."""
        if state["policy"] != self.policy:
            raise Refused(f"operation {self.meta['operation']} was recorded with another ledger policy; use a new operation")
        easyconfig = dict(self.meta["easyconfig"])
        if os.path.lexists(easyconfig["path"]) or "sha256" not in easyconfig:
            if not os.path.lexists(easyconfig["path"]):
                easyconfig["sha256"] = state["metadata"]["easyconfig"].get("sha256")
            else:
                easyconfig = _resolve_inputs({"easyconfig": easyconfig})["easyconfig"]
        if dict(self.meta, easyconfig=easyconfig) != state["metadata"]:
            raise Refused(f"operation {self.meta['operation']} was recorded with different metadata; use a new operation")
        self.meta = state["metadata"]

    def deliver(self, records, step):
        if records:
            outbox.enqueue(self.dir, records, self.faults, step)
        return outbox.deliver(self.dir, self.publisher, self.faults)

    def started_id(self):
        for event in outbox.load(self.dir)["events"]:
            if event["event_id"] == event_id(self.meta["operation"], "started") and event["status"] == "acknowledged":
                return event["receipt"]["id"]
        return None


def _pending_text(left):
    return f"{len(left)} ledger event(s) not acknowledged ({left[0]['error']})"


def _locked(config, meta, faults, body):
    os.makedirs(config["outbox"], mode=0o700, exist_ok=True)
    op = _Operation(config, meta, faults)
    if os.path.lexists(op.dir) and (os.path.islink(op.dir) or not os.path.isdir(op.dir)):
        raise Refused(f"operation directory {op.dir} is not a plain directory")
    lock = FileLock(op.dir)
    lock.acquire(config["lock_timeout"])
    try:
        if os.path.realpath(op.dir) != op.dir or not os.path.isdir(op.dir):
            raise Refused(f"operation directory {op.dir} is not a plain directory")
        return body(op)
    finally:
        lock.release()


def _finish(op, state, recorded=False):
    """Queue the terminal record held in ``state`` and deliver what is pending."""
    left = op.deliver([state["started"], state["terminal"]], "outbox.terminal")
    status, problem = state["result"], state.get("problem")
    code = _exit_code(status, problem)
    notes = [f"{state['terminal']['stage']}: {_reason(status)}"]
    if problem:
        notes.append(problem)
    if recorded:
        notes.append("recorded by an earlier attempt; the command was not repeated")
    if left:
        notes.append("ledger error: " + _pending_text(left) + f"; retry to deliver from {op.dir}")
        if code == 0:
            code = PENDING_EXIT
    return Outcome(code, state["phase"], status, len(left), "; ".join(notes))


def _record_exit(op, state, status, clock):
    terminal, problem = terminal_record(op.meta, clock(), status, op.started_id(), state.get("previous_modulefile"))
    state.update(phase="exited", result=status, terminal=terminal, problem=problem)
    op.save(state, "exited")


def run_build(config, metadata, argv, faults=None, clock=time.time, env=None):
    """Record ``started``, run ``argv``, then record its result. Returns an ``Outcome``.

    A repeated call for an operation whose command already ran delivers what
    is pending and returns the recorded result without running ``argv``.
    """
    if not argv:
        raise Refused("pass the command after --")
    config, meta = check_config(config), check_metadata(metadata)
    faults = faults or FaultInjector()

    def body(op):
        state = op.load()
        if state is None:
            _resolve_inputs(meta)
            previous = None
            if "modulefile" in meta and os.path.lexists(meta["modulefile"]["path"]):
                try:
                    previous = _hash_file(meta["modulefile"]["path"])[0]
                except OSError as exc:
                    raise Refused(f"existing module file {meta['modulefile']['path']} cannot be hashed: {exc}") from exc
            state = {
                "schema": SCHEMA, "operation": meta["operation"], "phase": "created",
                "policy": op.policy, "metadata": meta, "previous_modulefile": previous,
                "started": started_record(meta, clock(), previous),
                "result": None, "terminal": None, "problem": None,
            }
            op.save(state, "created")
        else:
            op.check(state)
        if state["phase"] == "exited":
            return _finish(op, state, recorded=True)
        if state["phase"] == "running":
            _record_exit(op, state, {"outcome": "unknown", "exit_status": None}, clock)
            return _finish(op, state)
        left = op.deliver([state["started"]], "outbox.started")
        if left and config["required"]:
            message = "command not run: " + _pending_text(left) + f"; retry to deliver from {op.dir}"
            return Outcome(PENDING_EXIT, state["phase"], None, len(left), message)
        state["phase"] = "running"
        op.save(state, "running")
        try:
            code = run_command(list(argv), dict(os.environ) if env is None else env, faults, "build_events.command")
            status = {"outcome": "exited", "exit_status": code}
        except OSError as exc:
            if exc.filename != argv[0]:
                raise
            status = {"outcome": "not-started", "exit_status": None}
        _record_exit(op, state, status, clock)
        return _finish(op, state)

    if threading.current_thread() is not threading.main_thread():
        raise Refused("run_build must be called from the main thread")
    if not os.path.isfile("/proc/self/stat"):
        raise Refused("run_build requires /proc to confirm the command's process group stopped")
    return _locked(config, meta, faults, body)


def deliver(config, metadata, faults=None, clock=time.time):
    """Deliver pending events of a recorded operation without running anything."""
    config, meta = check_config(config), check_metadata(metadata)
    faults = faults or FaultInjector()

    def body(op):
        state = op.load()
        if state is None:
            raise Refused(f"operation {meta['operation']} has no recorded state in {op.dir}")
        op.check(state)
        if state["phase"] == "exited":
            return _finish(op, state, recorded=True)
        if state["phase"] == "running":
            _record_exit(op, state, {"outcome": "unknown", "exit_status": None}, clock)
            return _finish(op, state)
        left = op.deliver([state["started"]], "outbox.started")
        if left:
            return Outcome(PENDING_EXIT, state["phase"], None, len(left), "command not run: " + _pending_text(left))
        return Outcome(0, state["phase"], None, 0, "started acknowledged; the command has not run")

    return _locked(config, meta, faults, body)


class _Stopped(BaseException):
    def __init__(self, signum):
        super().__init__(signum)
        self.signum = signum


def _raise_stop(signum, _frame):
    raise _Stopped(signum)


def _parser():
    parser = argparse.ArgumentParser(
        prog="python3 -m eb_staged_rebuild.build_events",
        description="Record started/finished/aborted ledger events around one build command.",
    )
    parser.add_argument("--config", required=True, help="build-event config JSON (ledger, key, outbox)")
    parser.add_argument("--metadata", required=True, help="build-event metadata JSON for this operation")
    parser.add_argument("--deliver", action="store_true", help="deliver pending events without running a command")
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    command = []
    if "--" in argv:
        at = argv.index("--")
        argv, command = argv[:at], argv[at + 1:]
    args = _parser().parse_args(argv)
    if args.deliver == bool(command):
        print("build-events: pass either --deliver or a command after --", file=sys.stderr)
        return REFUSED_EXIT
    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(signum, _raise_stop)
    try:
        config, metadata = load_config(args.config), _read_json(args.metadata, "build-event metadata")
        if args.deliver:
            outcome = deliver(config, metadata)
        else:
            outcome = run_build(config, metadata, command)
    except (Refused, NeedsIntervention) as exc:
        print(f"build-events: refused: {exc}", file=sys.stderr)
        return REFUSED_EXIT
    except _Stopped as stop:
        print(f"build-events: stopped by signal {stop.signum}; pending events stay in the outbox", file=sys.stderr)
        return 128 + stop.signum
    print(f"build-events: {outcome.message}", file=sys.stderr)
    return outcome.returncode


if __name__ == "__main__":
    raise SystemExit(main())

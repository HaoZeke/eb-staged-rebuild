"""Bounded adapter for ``eb-stack ledger append``.

The only coupling to eb-stack is its command line and the first line it
prints, the receipt; the tile log and checkpoint lines after it are not read.
A receipt is returned only when the command exits 0, its summary parses, and the
head file it wrote names the reported size and root. With ``public_key`` the
receipt also needs ``eb-stack ledger verify`` to prove the record is in that
signed head. Any other outcome is ambiguous: the record may or may not be in
the log, so the caller retries the same record and ``build.event_id`` makes
the retry idempotent.
"""

import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import time

from eb_staged_rebuild.builders import _group_running
from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.outbox import PublisherError

_SUMMARY = re.compile(
    r"^(appended|already recorded) record (\d+) id ([0-9a-f]{64}); "
    r"head size (\d+) root ([0-9a-f]{64})(?:; .*)?$"
)
_OUTPUT_LIMIT = 64 * 1024
_READ_SIZE = 64 * 1024
# Enough reads to empty a pipe grown to the unprivileged size ceiling (1 MiB).
_DRAIN_READS = 16
_FIELDS = ("actor", "stack", "target")
_ACTIONS = ("install", "rebuild-inplace", "rebuild-bump")

def _utc(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))

def publication_records(config, journal, stage, now, corrects=None):
    """One ledger record per published module file.

    ``module`` and ``location`` name the stable ``software/<name>/<version>``
    directory. That is the path a directory swap publishes and the path an
    RPATH built for the live prefix contains. The stage and the hidden
    retired sibling are not the published location.

    ``corrects`` maps a module file to the ledger id of its ``finished``
    record; the records are then the ``correction`` stages of a rollback.
    """
    scope = hashlib.sha256(journal["state_root"].encode("utf-8")).hexdigest()[:16]
    records = []
    for mod in journal["modules_plan"]:
        if "live_software" not in mod:
            raise Refused(
                "ledger records require a directory-swap plan; "
                "a generation-pointer journal is not published to the ledger"
            )
        if corrects is not None and mod["rel"] not in corrects:
            continue
        new, old = mod["new_hash"], mod["old_hash"]
        if corrects is not None:
            new, old = old, new
        name = "rollback-" + stage if corrects is not None else stage
        record = {
            "ts": _utc(now),
            "actor": config["actor"],
            "stack": config["stack"],
            "target": config["target"],
            "module": os.path.relpath(mod["live_software"], journal["software"]).replace(os.sep, "/"),
            "location": mod["live_software"],
            "action": "correction" if corrects is not None else config["action"],
            "stage": stage,
            "modulefile": {"path": mod["live_module"], "sha256": new, "previous_sha256": old},
            "build": {
                "event_id": f"eb-staged-rebuild/{scope}/{journal['run_id']}/{mod['rel']}/{name}",
                "run_id": journal["run_id"],
                "module_file": mod["rel"],
                "tool": "eb-staged-rebuild",
            },
        }
        if corrects is not None:
            record["corrects"] = corrects[mod["rel"]]
            record["reason"] = f"run {journal['run_id']} was rolled back"
        records.append(record)
    return records

def load_config(path):
    path = os.path.abspath(path)
    try:
        with open(path, encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, ValueError) as exc:
        raise Refused(f"cannot read ledger config {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise Refused(f"ledger config {path} is not a JSON object")
    for key in ("eb_stack", "log", "key", "head"):
        value = config.get(key)
        if not isinstance(value, str) or not os.path.isabs(value):
            raise Refused(f"ledger config {key} must be an absolute path")
    if config.get("public_key") is not None and not os.path.isabs(str(config["public_key"])):
        raise Refused("ledger config public_key must be an absolute path")
    for key in _FIELDS:
        if not isinstance(config.get(key), str) or not config[key]:
            raise Refused(f"ledger config {key} must be a non-empty string")
    config.setdefault("action", "rebuild-inplace")
    if config["action"] not in _ACTIONS:
        raise Refused(f"ledger config action must be one of {', '.join(_ACTIONS)}")
    config.setdefault("required", True)
    if not isinstance(config["required"], bool):
        raise Refused("ledger config required must be true or false")
    config.setdefault("timeout", 120)
    timeout = config["timeout"]
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise Refused("ledger config timeout must be a positive number of seconds")
    config["path"] = path
    return config

def _stop_group(child):
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if os.path.isdir("/proc"):
        while _group_running(child.pid):
            time.sleep(0.01)

def _keep(fd, kept, reads=1):
    """Read at most ``reads`` chunks, keeping the stream's first ``_OUTPUT_LIMIT`` bytes."""
    for _ in range(reads):
        try:
            chunk = os.read(fd, _READ_SIZE)
        except BlockingIOError:
            return True
        if not chunk:
            return False
        kept += chunk[: _OUTPUT_LIMIT - len(kept)]
    return True

def _bounded(argv, timeout):
    """Run ``argv`` in its own process group; return its code and leading output.

    Output goes through pipes: each stream keeps its first ``_OUTPUT_LIMIT``
    bytes in memory and the rest is read and dropped, so nothing reaches
    disk. The group is killed, and every member has exited, before this
    returns or raises, including on timeout and interruption; the unreaped
    leader reserves the group identifier until then. A writer that left the
    group is not waited for: it gets EPIPE once the pipes close, as does a
    helper whose caller was killed.
    """
    pipes = [os.pipe(), os.pipe()]
    kept = (bytearray(), bytearray())
    child = None
    try:
        try:
            child = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=pipes[0][1], stderr=pipes[1][1],
                start_new_session=True,
            )
        except OSError as exc:
            raise PublisherError(f"cannot start {argv[0]}: {exc}") from exc
        finally:
            for _read, write in pipes:
                os.close(write)
        with selectors.DefaultSelector() as selector:
            for index, (read, _write) in enumerate(pipes):
                os.set_blocking(read, False)
                selector.register(read, selectors.EVENT_READ, kept[index])
            deadline = time.monotonic() + timeout
            while os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PublisherError(f"{argv[0]} {argv[1]} {argv[2]} timed out after {timeout}s")
                if not selector.get_map():
                    time.sleep(min(remaining, 0.02))
                    continue
                for key, _events in selector.select(min(remaining, 0.05)):
                    if not _keep(key.fd, key.data):
                        selector.unregister(key.fd)
            _stop_group(child)
            for key in list(selector.get_map().values()):
                _keep(key.fd, key.data, _DRAIN_READS)
        code = child.wait()
    finally:
        try:
            if child is not None and child.returncode is None:
                _stop_group(child)
                child.wait()
        finally:
            for read, _write in pipes:
                os.close(read)
    return code, kept[0].decode("utf-8", "replace"), kept[1].decode("utf-8", "replace")

def _logged_event(log, index):
    try:
        with open(log, "rb") as handle:
            for number, line in enumerate(handle):
                if number == index:
                    build = json.loads(line).get("build")
                    return build.get("event_id") if isinstance(build, dict) else None
    except (OSError, ValueError) as exc:
        raise PublisherError(f"cannot read ledger {log}: {exc}") from exc
    return None

class EbStackLedger:
    def __init__(self, config):
        self.config = config
        self.required = config["required"]

    def append(self, record_path):
        config = self.config
        argv = [
            config["eb_stack"], "ledger", "append", record_path,
            "--log", config["log"], "--key", config["key"], "--head-out", config["head"],
        ]
        code, stdout, stderr = _bounded(argv, config["timeout"])
        if code != 0:
            raise PublisherError(f"eb-stack ledger append exited {code}: {stderr.strip()[-500:]}")
        lines = stdout.strip().splitlines()
        match = _SUMMARY.match(lines[0]) if lines else None
        if match is None:
            raise PublisherError(f"eb-stack ledger append printed no receipt: {stdout.strip()[-500:]!r}")
        status, index, record_id, size, root = match.groups()
        index, size = int(index), int(size)
        try:
            with open(config["head"], encoding="utf-8") as handle:
                head = json.load(handle)
        except (OSError, ValueError) as exc:
            raise PublisherError(f"cannot read head {config['head']}: {exc}") from exc
        if head.get("size") != size or head.get("root") != root or index >= size:
            raise PublisherError(
                f"head {config['head']} is size {head.get('size')} root {head.get('root')}, "
                f"not the reported size {size} root {root} covering record {index}"
            )
        with open(record_path, encoding="utf-8") as handle:
            event_id = json.load(handle)["build"]["event_id"]
        if _logged_event(config["log"], index) != event_id:
            raise PublisherError(f"ledger record {index} is not event {event_id}")
        if config.get("public_key"):
            verify = [
                config["eb_stack"], "ledger", "verify", "--log", config["log"],
                "--public-key", config["public_key"], "--record", str(index),
                "--head", config["head"],
            ]
            code, _out, stderr = _bounded(verify, config["timeout"])
            if code != 0:
                raise PublisherError(f"eb-stack ledger verify exited {code}: {stderr.strip()[-500:]}")
        return {
            "index": index,
            "id": record_id,
            "head_size": size,
            "head_root": root,
            "replayed": status == "already recorded",
            "verified": bool(config.get("public_key")),
        }

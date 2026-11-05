"""Bounded adapter for ``eb-stack ledger append``.

The only coupling to eb-stack is its command line and the line it prints. A
receipt is returned only when the command exits 0, its summary parses, and the
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
import signal
import subprocess
import tempfile
import time

from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.outbox import PublisherError

_SUMMARY = re.compile(
    r"^(appended|already recorded) record (\d+) id ([0-9a-f]{64}); "
    r"head size (\d+) root ([0-9a-f]{64})(?:; .*)?$"
)
_OUTPUT_LIMIT = 64 * 1024
_FIELDS = ("actor", "stack", "target")
_ACTIONS = ("install", "rebuild-inplace", "rebuild-bump")

def _utc(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))

def publication_records(config, journal, stage, now, corrects=None):
    """One ledger record per published module file.

    ``corrects`` maps a module file to the ledger id of its ``finished``
    record; the records are then the ``correction`` stages of a rollback.
    """
    scope = hashlib.sha256(journal["state_root"].encode("utf-8")).hexdigest()[:16]
    records = []
    for mod in journal["modules_plan"]:
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
            "module": os.path.relpath(mod["generation"], journal["stage_software"]).replace(os.sep, "/"),
            "location": mod["generation"],
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

def _bounded(argv, timeout):
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            child = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True
            )
        except OSError as exc:
            raise PublisherError(f"cannot start {argv[0]}: {exc}") from exc
        try:
            code = child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
            raise PublisherError(f"{argv[0]} {argv[1]} {argv[2]} timed out after {timeout}s")
        texts = []
        for handle in (out, err):
            handle.seek(0)
            texts.append(handle.read(_OUTPUT_LIMIT).decode("utf-8", "replace"))
    return code, texts[0], texts[1]

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
        match = _SUMMARY.match(lines[-1]) if lines else None
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

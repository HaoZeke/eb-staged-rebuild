"""Crash-safe run journal. Readers only ever see a fully written JSON file."""

import json
import os

from eb_staged_rebuild.atomicio import atomic_write, read_bytes
from eb_staged_rebuild.errors import NeedsIntervention, Refused

SCHEMA = 2
LEDGER_SCHEMA = 3


def journal_path(state_root, run_id):
    return os.path.join(os.fspath(state_root), "runs", run_id, "journal.json")


def run_dir(state_root, run_id):
    return os.path.join(os.fspath(state_root), "runs", run_id)


def generation_root(state_root, run_id):
    return os.path.join(os.fspath(state_root), "generations", run_id)


def save_journal(path, data, faults, step):
    data["schema"] = LEDGER_SCHEMA if data.get("ledger_config") else SCHEMA
    raw = json.dumps(data, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    atomic_write(path, raw, faults, step)


def load_journal(path):
    if not os.path.isfile(path) or os.path.islink(path):
        raise Refused(f"no journal at {path}")
    try:
        data = json.loads(read_bytes(path).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NeedsIntervention(f"journal cannot be read: {path}: {exc}") from exc
    if data.get("schema") not in (1, SCHEMA, LEDGER_SCHEMA):
        raise NeedsIntervention(
            f"journal schema {data.get('schema')!r} is not supported (1, {SCHEMA} or {LEDGER_SCHEMA})"
        )
    return data


def list_journals(state_root):
    runs = os.path.join(os.fspath(state_root), "runs")
    found = []
    if not os.path.isdir(runs):
        return found
    for name in sorted(os.listdir(runs)):
        path = os.path.join(runs, name, "journal.json")
        if os.path.isfile(path) and not os.path.islink(path):
            found.append(load_journal(path))
    return found

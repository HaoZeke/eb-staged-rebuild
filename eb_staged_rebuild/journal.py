"""Crash-safe run journal. Readers only ever see a fully written JSON file."""

import json
import os

from eb_staged_rebuild.atomicio import atomic_write, read_bytes
from eb_staged_rebuild.errors import NeedsIntervention, Refused

# Schema 2 records a directory swap: stable live path, staged path, retired sibling.
# Schema 1 is the generation-pointer journal this line refuses to interpret.
# Schema 3 is that unmerged line's ledger journal. It is not a directory swap.
# Schema 4 is a directory-swap journal whose ledger policy is frozen.
SCHEMA = 2
LEDGER_SCHEMA = 4
GENERATION_POINTER_SCHEMAS = (1, 3)


def journal_path(state_root, run_id):
    return os.path.join(os.fspath(state_root), "runs", run_id, "journal.json")


def run_dir(state_root, run_id):
    return os.path.join(os.fspath(state_root), "runs", run_id)


def generation_root(state_root, run_id):
    return os.path.join(os.fspath(state_root), "generations", run_id)


def save_journal(path, data, faults, step):
    if data.get("kind") == "retire":
        data["schema"] = SCHEMA
    elif data.get("ledger_policy"):
        data["schema"] = LEDGER_SCHEMA
        data["publication"] = "directory-swap"
    else:
        data["schema"] = SCHEMA
        if data.get("publication") is None:
            data["publication"] = "directory-swap"
    _require_directory_swap(data)
    raw = json.dumps(data, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    atomic_write(path, raw, faults, step)


def _require_directory_swap(data):
    publication = data.get("publication")
    if publication not in (None, "directory-swap"):
        raise NeedsIntervention(
            f"journal publication {publication!r} is not a directory swap"
        )
    if data.get("kind") == "retire":
        return
    for mod in data.get("modules_plan") or []:
        if "live_software" not in mod or "retired" not in mod:
            raise NeedsIntervention(
                "journal does not record a directory swap; "
                "a generation-pointer plan is not applied"
            )


def load_journal(path):
    if not os.path.isfile(path) or os.path.islink(path):
        raise Refused(f"no journal at {path}")
    try:
        data = json.loads(read_bytes(path).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NeedsIntervention(f"journal cannot be read: {path}: {exc}") from exc
    schema = data.get("schema")
    if schema in GENERATION_POINTER_SCHEMAS:
        raise NeedsIntervention(
            f"journal schema {schema} is a generation-pointer record"
            + (" and carries that line's ledger policy" if schema == 3 else "")
            + "; this program publishes by directory swap and will not apply it"
        )
    if schema == LEDGER_SCHEMA:
        if data.get("publication") != "directory-swap" or not isinstance(data.get("ledger_policy"), dict):
            raise NeedsIntervention(
                "schema 4 journal is not a frozen directory-swap ledger record"
            )
    elif schema != SCHEMA:
        raise NeedsIntervention(f"journal schema {schema!r} is not {SCHEMA} or {LEDGER_SCHEMA}")
    _require_directory_swap(data)
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

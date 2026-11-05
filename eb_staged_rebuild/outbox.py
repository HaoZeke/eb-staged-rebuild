"""Local outbox of publication events, one file per run.

Records are written durably before any delivery and are delivered one at a
time in the order they were queued. An event is acknowledged only with the
publisher's receipt. A failed or ambiguous delivery leaves the event pending
with its record unchanged, so the retry carries the same ``build.event_id``
and the same bytes, which the publisher acknowledges at most once.
"""

import json
import os

from eb_staged_rebuild.atomicio import atomic_write, read_bytes
from eb_staged_rebuild.errors import NeedsIntervention

SCHEMA = 1
PENDING_EXIT = 75

class PublisherError(Exception):
    """The publisher gave no receipt. The record may or may not be published."""

def outbox_path(run_dir):
    return os.path.join(run_dir, "outbox.json")

def load(run_dir):
    path = outbox_path(run_dir)
    if not os.path.lexists(path):
        return {"schema": SCHEMA, "events": []}
    try:
        data = json.loads(read_bytes(path).decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise NeedsIntervention(f"outbox cannot be read: {path}: {exc}") from exc
    if data.get("schema") != SCHEMA:
        raise NeedsIntervention(f"outbox schema {data.get('schema')!r} is not {SCHEMA}")
    return data

def _save(run_dir, data, faults, step):
    raw = json.dumps(data, indent=2, sort_keys=True) + "\n"
    atomic_write(outbox_path(run_dir), raw, faults, step)

def pending(data):
    return [event for event in data["events"] if event["status"] != "acknowledged"]

def enqueue(run_dir, records, faults, step):
    """Queue records whose ``build.event_id`` is new. Durable when this returns."""
    data = load(run_dir)
    known = {event["event_id"] for event in data["events"]}
    added = False
    for record in records:
        event_id = record["build"]["event_id"]
        if event_id in known:
            continue
        data["events"].append(
            {
                "seq": len(data["events"]),
                "event_id": event_id,
                "record": record,
                "status": "pending",
                "attempts": 0,
                "error": None,
                "receipt": None,
            }
        )
        known.add(event_id)
        added = True
    if added:
        _save(run_dir, data, faults, step)
    return data

def deliver(run_dir, publisher, faults):
    """Deliver pending events in order and stop at the first without a receipt.

    Returns the events still pending. Fault points: ``ledger.append:<id>``
    before the publisher runs and ``ledger.acked:<id>`` after its receipt,
    before the receipt is saved.
    """
    data = load(run_dir)
    for event in data["events"]:
        if event["status"] == "acknowledged":
            continue
        event_id = event["event_id"]
        record_path = os.path.join(run_dir, "events", f"{event['seq']:04d}.json")
        body = (json.dumps(event["record"], sort_keys=True) + "\n").encode("utf-8")
        if not os.path.isfile(record_path) or read_bytes(record_path) != body:
            atomic_write(record_path, body, faults, f"outbox.record:{event_id}")
        faults.reach(f"ledger.append:{event_id}")
        event["attempts"] += 1
        try:
            receipt = publisher.append(record_path)
        except (PublisherError, OSError) as exc:
            event["error"] = str(exc)
            _save(run_dir, data, faults, f"outbox.failed:{event_id}")
            return pending(data)
        faults.reach(f"ledger.acked:{event_id}")
        event["status"] = "acknowledged"
        event["error"] = None
        event["receipt"] = receipt
        _save(run_dir, data, faults, f"outbox.acked:{event_id}")
    return []

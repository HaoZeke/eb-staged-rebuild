"""Stand-in for ``eb-stack ledger append`` and ``ledger verify``.

It keeps an idempotent JSON Lines log keyed by ``build.event_id`` and prints
eb-stack's summary line. ``FAKE_LEDGER_PLAN`` names a file with one mode per
line, consumed one per append: ``ok``, ``fail-before`` (exit 1, nothing
written), ``fail-after-append`` (the line is written, the head is not),
``garbage`` (exit 0 with no summary), ``stale-head`` (exit 0, head not
updated) or ``hang`` (sleep until killed).
"""

import hashlib
import json
import os
import sys
import time

def _mode():
    plan = os.environ.get("FAKE_LEDGER_PLAN")
    if not plan or not os.path.exists(plan):
        return "ok"
    with open(plan) as handle:
        lines = handle.read().splitlines()
    if not lines:
        return "ok"
    with open(plan, "w") as handle:
        handle.write("".join(line + "\n" for line in lines[1:]))
    return lines[0].strip()

def _flag(argv, name):
    return argv[argv.index(name) + 1]

def _lines(log):
    if not os.path.exists(log):
        return []
    with open(log, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]

def _write_head(path, lines, log):
    root = hashlib.sha256(open(log, "rb").read()).hexdigest() if lines else "0" * 64
    with open(path + ".tmp", "w") as handle:
        json.dump({"domain": "fake", "size": len(lines), "root": root}, handle)
    os.replace(path + ".tmp", path)
    return root

def append(argv):
    record_path = argv[0]
    log, head = _flag(argv, "--log"), _flag(argv, "--head-out")
    mode = _mode()
    if mode == "hang":
        time.sleep(3600)
    if mode == "fail-before":
        print("error: injected failure before the append", file=sys.stderr)
        return 1
    with open(record_path, encoding="utf-8") as handle:
        record = json.load(handle)
    event = record["build"]["event_id"]
    lines = _lines(log)
    previous = [line for line in lines if line["build"]["event_id"] == event]
    if previous:
        body = dict(previous[0])
        index = body.pop("index")
        body.pop("id")
        if body != record:
            print(f"error: build.event_id {event!r} is already recorded with different content", file=sys.stderr)
            return 1
        status = "already recorded"
        entry = previous[0]
    else:
        index = len(lines)
        entry = dict(record, index=index)
        entry["id"] = hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        lines.append(entry)
        status = "appended"
    if mode == "fail-after-append":
        print("error: injected failure writing the head", file=sys.stderr)
        return 1
    if mode == "stale-head":
        root = hashlib.sha256(open(log, "rb").read()).hexdigest()
    else:
        root = _write_head(head, lines, log)
    if mode == "garbage":
        print("done")
        return 0
    print(f"{status} record {index} id {entry['id']}; head size {len(lines)} root {root}")
    return 0

def verify(argv):
    with open(_flag(argv, "--head"), encoding="utf-8") as handle:
        head = json.load(handle)
    return 0 if int(_flag(argv, "--record")) < head["size"] else 1

def main(argv):
    if argv[:2] == ["ledger", "append"]:
        return append(argv[2:])
    if argv[:2] == ["ledger", "verify"]:
        return verify(argv[2:])
    print(f"unsupported: {argv}", file=sys.stderr)
    return 2

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

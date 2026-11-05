# Build events

`eb_staged_rebuild.build_events` records one software-build command in an
eb-stack ledger. It is opt-in and independent of `run`/`resume`: any shell
pipeline step can wrap its build command with it.

```bash
python3 -m eb_staged_rebuild.build_events \
  --config /etc/build-events/ledger.json \
  --metadata "$WORKSPACE/build-event.json" \
  -- eb zlib-1.3.1-GCCcore-14.2.0.eb --robot
```

The command after `--` runs with the caller's environment, stdin, stdout and
stderr. Its output is not captured or stored. A `started` record must be
acknowledged by `eb-stack ledger append` before the command runs. Afterwards
a `finished` record (exit 0 and every declared output checked) or an
`aborted` record (any other result) is appended.

## Configuration

| Key | Content |
| --- | --- |
| `eb_stack` | Absolute path of the `eb-stack` executable |
| `log` | Absolute path of the ledger's JSON Lines file |
| `key` | Absolute path of the Ed25519 PKCS#8 signing key. Refused inside the ledger directory or the outbox |
| `outbox` | Absolute directory for durable producer state; disjoint from the ledger directory and reached without a symbolic link |
| `public_key` | Optional. With it, each receipt also needs `eb-stack ledger verify` to prove inclusion |
| `required` | `true` by default: the command does not run until `started` is acknowledged |
| `timeout` | Seconds per eb-stack call (120) |
| `lock_timeout` | Seconds to wait for another writer of the same operation (0: refuse at once) |

Paths must be absolute and normalized. Unknown keys are refused. There is no
shared `head` setting. Each operation writes its own signed head,
`<outbox>/<operation>/head.json`, so concurrent writers cannot replace the
head another writer is validating. Use `eb-stack ledger head` to publish an
anchor.

## Metadata

Only these fields are read, and anything else is refused, at every depth.
The environment, the command, its arguments and licence values are never
serialized.

| Key | Content |
| --- | --- |
| `operation` | Stable identity, `[A-Za-z0-9][A-Za-z0-9._-]*` (max 128, no `..`). Event ids are `eb-build-events/<operation>/<stage>` |
| `actor`, `stack`, `module` | Ledger fields |
| `location` | Absolute installation path |
| `target` | `{"cpu": "x86_64/amd/zen4", "accelerator": "nvidia/cc90"}`; the ledger `target` is `cpu` or `cpu/accel/accelerator` |
| `action` | `install` (default), `rebuild-inplace` or `rebuild-bump` |
| `easyconfig` | `{"path", "sha256"}`. A readable file is hashed and must match a supplied hash |
| `sources`, `patches` | Lists of `{"name", "sha256"}`. A name is a plain file name |
| `modulefile` | `{"path", "sha256"?}`. Hashed after a zero exit; a supplied hash must match. The hash before the build goes in `previous_sha256` |
| `tool` | `{"name", "version"?, "revision"?}` of the build tool |
| `logs` | Lists of `{"uri", "sha256"?}`. A URI with credentials, a query or a fragment is refused |
| `artifact` | `{"accession": "sha256:<hex>"?, "path"?, "uri"?}`. A path is hashed after a zero exit into the accession and size |

The records use only the current ledger fields. `easyconfig`, `sources`
(`files`, `patches`), `modulefile` and `build` carry the references. `build`
carries `event_id`, `operation`, `producer`, `cpu_target`, `accelerator`,
`tool`, `logs`, `artifact`, `outcome`, `exit_status`, and `started_id` (the
ledger id of the acknowledged `started` record).

## Delivery and retry

Records are written to `<outbox>/<operation>/outbox.json` before delivery
and delivered in order. The ledger adapter bounds each call's output, stops
the call's whole process group, and accepts a receipt only when eb-stack exits
zero, its summary parses, the head names that size and root, and the logged
line carries the event id. Anything else leaves the event pending with its
bytes unchanged. Retrying the same command line delivers the same record,
which eb-stack acknowledges once as `already recorded`. A retry with
different metadata or ledger policy is refused locally. The same operation
with a different payload from another outbox is refused by the ledger.

`operation.json` records the phase. A retry never repeats a command that
already ran. It returns the recorded result after delivering what is
pending. If the producer stopped while the command was running (SIGKILL,
host loss), the result is unknown: `aborted` with outcome `unknown` is
recorded and the command is not repeated. Stop the whole job before retrying.

`--deliver` (instead of a command) only delivers pending events.

## Exit status

| Code | When |
| --- | --- |
| 0 | The command exited 0 and every event is acknowledged |
| command's status | The command failed. Pending events are named on stderr as a ledger error |
| 75 | An event is not acknowledged. In required mode before the command, the command did not run |
| 65 | The command exited 0 but a declared module file or artifact is missing or does not match |
| 70 | The previous producer stopped during the command; the result is unknown |
| 127 | The command could not be started |
| 128+N | Stopped by signal N. The command's group was stopped first |
| 2 | Configuration, metadata, path or lock refused; nothing was appended |

## Python API

```python
from eb_staged_rebuild import build_events

config = build_events.load_config("/etc/build-events/ledger.json")
outcome = build_events.run_build(config, metadata_dict, ["eb", "zlib.eb"])
outcome = build_events.deliver(config, metadata_dict)
# Outcome(returncode, phase, command_status, pending, message)
```

Call from the main thread on a host with `/proc`. The command runs in its own
process group, and every member has exited before the call returns. SIGTERM,
SIGINT and SIGHUP during the command stop the group (after a two-second grace)
and record `aborted`. The module command line also stops an in-flight
eb-stack call on those signals, leaving its event pending.

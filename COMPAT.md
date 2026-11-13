# The Zig build and the Python tool

The Zig build in `zig/` is being ported command by command, in the order the
port plan sets. In this build: `probe`, `status`, `resolve`, `retire`, `gc`,
and `rollback` of a retire journal; `run --dry-run`. Still to come: the ledger maintenance entry point; then `run`,
`resume` and `rollback` of a run journal, which publishes through the ledger
outbox as `run` does. A command not yet ported exits 2 with
`eb-staged-rebuild: COMMAND is not in this build yet`. The Python tool stays
the one to use until every command is ported and the test suite passes
against the binary through `TOOL_BIN`.

`tests/differential.sh` runs both on the same invocations and fails on any
difference in exit code, stdout or stderr; for `retire`, `gc` and `rollback` it
also compares the journals and the trees they leave, with run ids and
timestamps normalized.

## Where the Zig build differs on purpose

| case | Python tool | Zig build |
|---|---|---|
| `--help` text | wrapped to the terminal width (`COLUMNS`) | the 80-column text |
| usage lines and argparse errors | as the running Python's argparse prints them, which changes between releases | as Python 3.14 prints them |
| a module file or journal that is not UTF-8 | a Python traceback, exit 1 | `eb-staged-rebuild: PATH is not UTF-8 text`, exit 2 for `resolve`; `journal cannot be read: PATH: not UTF-8 text`, exit 2 for `status` |
| a journal that is not valid JSON | `journal cannot be read: PATH: ` and the JSON decoder's message | the same start, then `not valid JSON` |
| a journal value that `status` cannot format (for example a `null` status) | a Python traceback, exit 1 | `Traceback: TypeError: ...` on stderr, exit 1 |
| program output that is not UTF-8 (bwrap, eb) | a decoding error | decoded with U+FFFD for invalid bytes |
| `--lock-timeout` and `--grace-hours` | Python `float()`, which also takes underscores and non-ASCII digits | ASCII decimal and exponent forms |
| a lock wait longer than about 49 days | waits that long | capped at 2^32 ms |
| the user-namespace probe | `unshare ... python3 -m eb_staged_rebuild.userns_sandbox --probe` | `unshare ... <this binary> __userns-sandbox --probe` |

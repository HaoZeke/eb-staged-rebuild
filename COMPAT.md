# The Zig build and the Python tool

The Zig build in `zig/` is being ported command by command, in the order the
port plan sets. In this build: `probe`, `status`, `resolve`, `retire`, `gc`,
`run`, `resume` and `rollback` for runs without `--ledger-config`; the ledger
maintenance entry point as `ledger-maintenance`. Still to come: runs with
`--ledger-config`, which publish through the ledger outbox. Such a run exits 2
with `eb-staged-rebuild: ... is not in this build yet`. The Python tool stays
the one to use until every command is ported and the test suite passes
against the binary through `TOOL_BIN`.

`tests/differential.sh` runs both on the same invocations and fails on any
difference in exit code, stdout or stderr; for the commands that change a fixture
(`retire`, `gc`, `rollback`, `run`, `resume`) it also compares the journals and
the trees they leave, with timestamps and the sandbox command normalized, and,
for whole runs, the sha256 of module text that names each fixture's own path.

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
| the ledger maintenance entry point | `python -m eb_staged_rebuild.ledger_maintenance` | `eb-staged-rebuild ledger-maintenance`, the same four subcommands and options, exit codes 0, 2 and 75 |
| ledger, head or manifest text that is not valid JSON | `... is not JSON: ` and the JSON decoder's message | the same start, then the reason and the character offset |
| `\d` in timestamps, receipts and anchor names | any Unicode decimal digit | ASCII digits only |
| a manifest string with an unpaired `\uD800`-`\uDFFF` escape | accepted, and escaped again in the appended record | refused: `manifest FIELD holds an unpaired surrogate escape` |
| an integer in a head, record or manifest beyond 64 bits | accepted | refused: `... holds the integer N, which is out of range` |
| an operating-system error with no fixed text in the Zig build | `[Errno N] strerror: 'PATH'` | `ErrorName: 'PATH'` (`[Errno N] ...` for ENOENT, EACCES, EISDIR, ENOTDIR, ELOOP, EEXIST, ENOSPC, EROFS) |
| `ledger-maintenance SUBCOMMAND --help` | argparse's option help | a short list of the options and which are required |

# The Zig build and the Python tool

The Zig build in `zig/` carries every command of the Python tool: `probe`,
`status`, `resolve`, `retire`, `gc`, `run`, `resume` and `rollback`, with or
without `--ledger-config`, and the ledger maintenance entry point as
`ledger-maintenance`, and the `build_events` module's entry point
(`python3 -m eb_staged_rebuild.build_events`) as `build-events`. The Python
tool stays the one to use until the test suite passes against the binary
through `TOOL_BIN`.

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
| `build-events` usage errors | `usage: python3 -m eb_staged_rebuild.build_events ...` | `usage: eb-staged-rebuild build-events ...` |
| a signal while `build-events` runs outside the command | the exception unwinds, releasing the lock | the process prints the same message and exits 128 + signal; the kernel releases the lock |
| `build-events` metadata or config that is not valid JSON | the JSON decoder's message | `not valid JSON` |
| the user-namespace probe | `unshare ... python3 -m eb_staged_rebuild.userns_sandbox --probe` | `unshare ... <this binary> __userns-sandbox --probe` |
| the ledger maintenance entry point | `python -m eb_staged_rebuild.ledger_maintenance` | `eb-staged-rebuild ledger-maintenance`, the same four subcommands and options, exit codes 0, 2 and 75 |
| ledger, head or manifest text that is not valid JSON | `... is not JSON: ` and the JSON decoder's message | the same start, then the reason and the character offset |
| `\d` in timestamps, receipts and anchor names | any Unicode decimal digit | ASCII digits only |
| a manifest string with an unpaired `\uD800`-`\uDFFF` escape | accepted, and escaped again in the appended record | refused: `manifest FIELD holds an unpaired surrogate escape` |
| an integer in a head, record or manifest beyond 64 bits | accepted | refused: `... holds the integer N, which is out of range` |
| an operating-system error with no fixed text in the Zig build | `[Errno N] strerror: 'PATH'` | `ErrorName: 'PATH'` (`[Errno N] ...` for ENOENT, EACCES, EISDIR, ENOTDIR, ELOOP, EEXIST, ENOSPC, EROFS) |
| `ledger-maintenance SUBCOMMAND --help` | argparse's option help | a short list of the options and which are required |

# eb-staged-rebuild

Rebuild an EasyBuild install in a staged tree, then exchange the complete tree
with the stable `software/<name>/<version>` directory.

The default backend tries EasyBuild bubblewrap, then a user/mount namespace
that makes both live trees read-only, then an unsandboxed install-path
fallback. Publish uses `renameat2(RENAME_EXCHANGE)` when the target filesystem
supports it. The ordinary-rename fallback restores the old live name after
every caught failure.

Author: Rohit Goswami &lt;rgoswami@ieee.org&gt;. License: the Massachusetts Institute of Technology license.

## Install

Python 3.11 or newer is required. The standard library is sufficient.

```bash
python3 -m pip install .
python3 -m eb_staged_rebuild --help
```

## Probe and run

```bash
eb-staged-rebuild probe --path "$EASYBUILD_INSTALLPATH_SOFTWARE"

eb-staged-rebuild run \
  --software "$EASYBUILD_INSTALLPATH_SOFTWARE" \
  --modules "$EASYBUILD_INSTALLPATH_MODULES" \
  --dry-run \
  -- example.eb --robot

eb-staged-rebuild run \
  --software "$EASYBUILD_INSTALLPATH_SOFTWARE" \
  --modules "$EASYBUILD_INSTALLPATH_MODULES" \
  -- example.eb --robot
```

`--backend auto` is the default. The choices are `auto`, `bwrap`, `userns`,
and `installpath`. Use `--backend bwrap` or `--backend userns` when an
unsandboxed fallback must be forbidden. Use `--backend installpath` only when
the documented fallback is acceptable.

`--eb` selects the EasyBuild executable (environment `EB_STAGED_REBUILD_EB`,
default `eb`). `probe` learns whether that EasyBuild writes the stage paths
into `bwrap_info.json` by importing its `easybuild.tools.bwrap` with the
Python beside `eb`; when `eb` is a wrapper with no Python beside it, a table
read from the framework source answers for 5.3.0, 5.3.1 (no stage keys, the
5.3 layout), 5.4.0, 5.4.1 and 5.4.2 (keys present), and any other version
reads as unknown. `--run-id` names the journal instead of the generated id.
`--lock-timeout` is how long to wait for `<state-root>/.lock` and its POSIX
record-lock sidecar `<state-root>/.lock.posix`, in seconds. Omit it and the
wait has no deadline. `retire` defaults to 0. A 0.2.0 process takes only the
local flock lock. That process does not take the sidecar.

The `--prepare` option names an executable. That executable receives a
directory-swap context JSON before the live directory is exchanged.
`--ledger-config` names a JSON eb-stack ledger configuration; `started`
records are acknowledged before the swap, and the stable
`software/<name>/<version>` path is the record's `location`. `--module`
names a `Name/version` to publish when EasyBuild's dry-run does not.
`--skip-live-check` skips the before/after content record of the live install.
`gc --include-failed` also removes the hidden sibling recorded for a failed
run whose modules are still pending, once the grace period has passed.
`resolve` prints the install path a module file names.

Two modules run beside `run` rather than inside it. `python3 -m
eb_staged_rebuild.build_events -- COMMAND` records a `started` event in the
eb-stack ledger before one build command runs and a `finished` or `aborted`
event after it, through a durable outbox; see
[docs/build-events.md](docs/build-events.md). `python -m
eb_staged_rebuild.ledger_maintenance` backfills observed history into a
ledger, publishes daily signed anchors, proves the anchor chain, and projects
package history from a verified log; see
[docs/ledger-maintenance.md](docs/ledger-maintenance.md). Both speak to the
eb-stack 0.5.0 ledger CLI: the first line a command prints is its receipt, a
tile-log or checkpoint line follows it, and `verify` audits the tile log
beside the ledger.

The state directory defaults to a hidden sibling of the software tree. It must
be on the same filesystem as the live installs.

## Recovery

```bash
eb-staged-rebuild status --state-root /path/to/.eb-staged-rebuild
eb-staged-rebuild resume --state-root /path/to/.eb-staged-rebuild RUN_ID
eb-staged-rebuild rollback --state-root /path/to/.eb-staged-rebuild RUN_ID
eb-staged-rebuild gc --state-root /path/to/.eb-staged-rebuild --dry-run
```

`rollback` exchanges the retired directory back into the stable path. For a
retire run it puts the module file back. `gc` deletes only a journaled hidden
sibling after the grace period; it never deletes the stable live path. The
default grace is 144 hours, the notes' unverified 120 hours plus a day.

When a package installed here is later published elsewhere on `MODULEPATH`
(for example a CernVM File System repository), `retire` moves its module file out of the
module tree, so users load the other copy, without touching
`software/<name>/<version>`:

```bash
eb-staged-rebuild retire \
  --state-root /path/to/.eb-staged-rebuild \
  --module-file /path/to/modules/all/Name/version.lua \
  --reason "published on software.surf.nl" \
  --dry-run
```

Drop `--dry-run` to move the file. `rollback` with the printed
`retire-<timestamp>` id puts it back. `gc` lists the software directory as
ready to remove by hand once `--grace-hours` have passed. The state root must
be on the same filesystem as the module tree.

## Test

```bash
python3 -m unittest discover -s tests -v
```

Set `EB_STAGED_REBUILD_INTEGRATION=1` to enable the real EasyBuild/Lmod tests.
With that variable set, missing `eb` or Lmod is a failure. The bubblewrap case
skips when `bwrap` is not usable on that host.

The ledger tests need `EB_STACK_BIN`, an eb-stack 0.5.0 binary, and `openssl`
on `PATH`; `tests/test_ledger_eb_stack.py` skips without the binary, the
build-events and ledger-maintenance suites fail without it.

See [DESIGN.md](DESIGN.md), [the runbook](docs/runbook.md), and the Org-mode
[how-to](docs/orgmode/howto/staged-rebuild.org) and
[reference](docs/orgmode/reference/staged-rebuild.org).

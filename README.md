# eb-staged-rebuild

Rebuild an EasyBuild install without emptying the live prefix.

`eb --rebuild` and `eb --force` clear the install directory during the build. This tool installs into a new directory and then publishes that directory by replacing the module file. `software/<name>/<version>` stays a real directory. It is not renamed, and it is not replaced with a symlink.

The module file is the pointer. It is written to a temporary name in the same directory and moved into place with one `rename`. Jobs that already loaded the module keep the old paths. Jobs that load it afterwards see one complete generation. Old generations stay until a grace period has passed.

EasyBuild's `--installpath-software` and `--installpath-modules` are enough to build the new directory. `--bwrap` (EasyBuild 5.3 and later) is optional. The switch runs in this process, after `eb` exits, because hooks run inside the build and inside the sandbox when `--bwrap` is on.

Author: Rohit Goswami &lt;rgoswami@ieee.org&gt;. License: MIT.

## Install

Python 3.11 or newer. The standard library is enough.

```bash
python3 -m pip install .
# or, from this directory:
python3 -m eb_staged_rebuild --help
```

## Run

```bash
eb-staged-rebuild probe --path /path/to/software
eb-staged-rebuild run \
  --software "$EASYBUILD_INSTALLPATH_SOFTWARE" \
  --modules "$EASYBUILD_INSTALLPATH_MODULES" \
  --dry-run \
  -- file.eb --robot
eb-staged-rebuild run \
  --software "$EASYBUILD_INSTALLPATH_SOFTWARE" \
  --modules "$EASYBUILD_INSTALLPATH_MODULES" \
  -- file.eb --robot
```

`--software` and `--modules` are the live trees. They also come from `EASYBUILD_INSTALLPATH_SOFTWARE` and `EASYBUILD_INSTALLPATH_MODULES`. The state directory defaults to `<parent of software>/.eb-staged-rebuild`.

A site permission finalizer can run before any module becomes visible:

```bash
eb-staged-rebuild run --prepare /absolute/path/to/finalize-permissions -- file.eb --robot
```

The executable receives one argument: a JSON file naming the generation,
state and live roots, staged software and module roots, and each module's
software directory and prepared module file. The prepared file inherits
the existing module's ownership, mode and access ACL, or the staged module's
metadata for a new installation. The finalizer can adjust the prepared
file's access metadata; publication copies that metadata to the replacement
before the module becomes visible. Metadata errors refuse replacement.
Rollback restores the backed-up module's bytes and access metadata.
ACLs are inspected with `listxattr` and direct descriptor probes: `system.gpfs_nfs4_acl`
(GPFS native NFSv4), `system.nfs4_acl` (generic NFSv4) and
`system.posix_acl_access`. Their bytes are copied unchanged, never
translated; only the most native listed name is written, after ownership
and mode, and every listed name must read back identically. An ACL that
cannot be inspected, an unknown `system.*` attribute, an ACL that
changes before the rename, or a filesystem that cannot store the same
model refuses replacement. GPFS can return a virtual `system.nfs4_acl`
even when `listxattr` is empty; the known names are always probed and the
complete metadata snapshot is read twice. Prepared files and backups live
under the state root, so it must store the live modules' ACL model. Native
replacement and rollback still require filesystem-specific verification.
It applies and verifies the site's permissions in that generation and
returns zero to permit publication. A nonzero exit leaves live modules
unchanged. Every planned generation is checked after the finalizer and
before any module is published; an incomplete or unreadable generation
prevents the first publication. The finalizer runs outside the EasyBuild sandbox. It must be
idempotent because a crash can require repeating it. Its path and result
are retained in the run journal; resume runs an unfinished finalizer before
publication. A completed finalizer is not repeated while resuming module
publication. A finalizer failure retains the built generation, so retrying
does not repeat EasyBuild. An empty finalizer argument is refused. A child
terminated by a signal returns the corresponding shell exit status.
Build and preparation commands run in a dedicated process group. SIGTERM,
SIGINT and SIGHUP stop that group before the writer lock is released;
workers that ignore termination are killed after a two-second grace period.
The lock is held until `/proc` shows every killed group member has exited
and closed its files. Commands must not daemonize or move workers into
other process groups.
SIGKILL and host loss cannot run this cleanup; the scheduler must stop the
entire job before an operator resumes interrupted work.

Journals use schema 2 so a reader that lacks preparation support refuses
them. Schema 1 remains readable, and each journal write upgrades its schema.

Publication events can be recorded in an eb-stack ledger:

```bash
eb-staged-rebuild run --ledger-config /absolute/path/ledger.json -- file.eb
```

The config names `eb_stack`, `log`, `key` and `head` (absolute paths),
`actor`, `stack` and `target`, and optionally `public_key`, `action`
(`rebuild-inplace` by default), `timeout` in seconds (120) and `required`
(true). Each module gets a `started` record before its module file can
change, `finished` after the run completes, and `aborted` when a rollback
undoes a publication that never finished; rolling back a finished
publication records `correction` stages naming the `finished` record. The
records go to `runs/<run-id>/outbox.json` before delivery and are delivered
in order by running `eb-stack ledger append`. An event counts as
acknowledged only when eb-stack exits zero, its summary parses, the head
file names the reported size and root, the logged line carries the event's
`build.event_id`, and, with `public_key`, `eb-stack ledger verify` proves
inclusion. Otherwise the event stays pending with the same record and the
command exits 75: with `required`, no module file has changed; after
publication, `resume` delivers the rest without rebuilding or republishing.
`rollback` delivers pending events first and changes nothing while a
required one stays pending.


After a crash:

```bash
eb-staged-rebuild status --state-root /path/to/.eb-staged-rebuild
eb-staged-rebuild resume --state-root /path/to/.eb-staged-rebuild RUN_ID
eb-staged-rebuild rollback --state-root /path/to/.eb-staged-rebuild RUN_ID
```

`rollback` restores the previous module files and leaves the generation on disk. For a retire it puts the module file back. `gc` removes a retired generation only after `--grace-hours` (default 144, from the notes, not a measured site limit) and only under the state directory's `generations/` folder.

```bash
eb-staged-rebuild gc --state-root /path/to/.eb-staged-rebuild --dry-run
eb-staged-rebuild resolve /path/to/modules/all/Name/version.lua
```

When a package installed here is later published elsewhere on `MODULEPATH` (for example a CVMFS repository), `retire` moves its module file out of the module tree, so users load the other copy, without touching `software/<name>/<version>`:

```bash
eb-staged-rebuild retire \
  --state-root /path/to/.eb-staged-rebuild \
  --module-file /path/to/modules/all/Name/version.lua \
  --reason "published on software.surf.nl" \
  --dry-run
```

Drop `--dry-run` to move the file. `rollback` with the printed `retire-<timestamp>` id puts it back. `gc` lists the software directory as ready to remove by hand once `--grace-hours` have passed. The state root must be on the same filesystem as the module tree. Cleanup holds the publication lock while checking
references and removing a retired generation. It restores owner permissions
only inside the owned generation and unlinks symbolic links without following
their targets, so sealed EasyBuild directories can be removed safely.
An unreadable, undecodable or unrecognized tracked live module refuses
collection before any generation is removed. A missing module is distinct
from a reference that cannot be inspected.

The operator notes are in [docs/runbook.md](docs/runbook.md). The choice of design is in [DESIGN.md](DESIGN.md).

## Tests

```bash
python3 -m unittest discover -s tests
```

A real EasyBuild rebuild is skipped unless `EB_STAGED_REBUILD_INTEGRATION=1` and both `eb` and Lmod are on `PATH` (`LMOD_CMD` when Lmod is not already configured). With the variable set, a missing `eb` or Lmod is a failure.

## What this does not do

It does not make an in-place rebuild safe. It does not update `DT_RPATH` or scripts that embed the old directory. It does not claim `flock` or `RENAME_EXCHANGE` work on a filesystem this process has not probed. `open-004` (the site's bubblewrap, sysctl, rename flag, cross-node lock, and EasyBuild version) stays open.

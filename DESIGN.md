# Design

EasyBuild `--rebuild` and `--force` empty an existing install before the new
install has passed its sanity checks. This program never points EasyBuild at
the live install for a staged build. It builds a complete replacement, takes
one `flock` for the build and publish transaction, and then swaps the staged
directory with `software/<name>/<version>`.

The stable path remains a real directory. A successful rebuild changes the
directory inode and contents at that path; the retired directory is kept as
`software/<name>/.<version>.<run-id>` for rollback and for running processes
that still hold old files open.

## Build isolation

`--backend auto` selects these backends in order:

1. EasyBuild's experimental bubblewrap backend, after
   `bwrap --dev-bind / / true` succeeds.
2. `unshare` with a user and mount namespace. The helper bind-mounts the live
   software and module trees read-only, verifies that the probe mount rejects a
   write with `EROFS`, and runs an install-path build into the stage.
3. The install-path fallback. It still sends EasyBuild to a separate software
   and module tree, but it has no kernel sandbox. The command prints this
   downgrade. Sites that require sandboxing should select `--backend bwrap` or
   `--backend userns`; either explicit backend refuses instead of downgrading.

The fallback exists for hosts where bubblewrap is absent and unprivileged user
namespaces are disabled. It protects against EasyBuild's normal cleanup paths,
not against a hook that deliberately writes outside its configured install
path.

EasyBuild options that could redirect the build are removed:
`--prefix`, `--installpath*`, `--subdir-*`, `--bwrap*`, and
`--remove-ghost-install-dirs`. `--rebuild` is added unless `--rebuild`,
`--force`, or `-f` is already present.

## Publish transaction

For every module, planning:

* requires an existing, non-symlink live `Name/version` directory;
* verifies that the staged install is complete;
* writes a run marker into the staged install;
* prepares a module file whose root is the stable live path;
* records and durably backs up the current module file; and
* moves the stage to a hidden sibling on the same filesystem.

Publish first attempts Linux `renameat2` with `RENAME_EXCHANGE` (`1 << 1`,
from `include/uapi/linux/fs.h`). The syscall changes both directory names in
one operation. `EINVAL`, `ENOSYS`, or `EOPNOTSUPP` selects the shared-filesystem
fallback:

1. rename live to `<staged>.fallback-old`;
2. rename staged to live;
3. rename the hold path to staged.

Every caught error or injected failure restores the original two names before
it is re-raised. If a process or machine dies between fallback renames, the
journal and the hold-name state let `resume` restore the old live name before
trying again. This recovery cannot make the fallback's transient missing-name
window atomic; only `RENAME_EXCHANGE` can do that. A deployment that requires
an uninterrupted name must require a filesystem where the exchange probe
succeeds.

For a multi-module build, all directory swaps happen before module files are
published. If any swap, module write, class-link operation, or journal write
fails, every completed module-file update and directory swap is reversed.
Tests inject a failure before every recorded operation and verify that every
live directory has its original inode and bytes afterwards.

The module-file replacement is a same-directory `os.replace` after file and
directory `fsync`. The directory swap is still not one atomic operation across
several modules. Other users do not take this tool's advisory lock, so they can
observe a mixed set while a successful multi-module transaction is in flight.

## Lock, resume, rollback, and collection

One `flock` on `<state-root>/.lock` covers build, publish, resume, rollback,
garbage collection, and their journal changes. It serialises processes that
observe the same lock implementation. Cross-node coherence remains an
`open-004` measurement.

The journal records the stage, stable path, retired path, hashes, and completed
steps. `resume` is idempotent. It first recovers an interrupted fallback and
normalises any interrupted publish to the old tree.

`rollback` exchanges each retired directory back into the stable path in
reverse order and restores module files. A failed rollback reverses its own
completed operations, leaving the published tree live.

`gc` only removes the hidden retired sibling recorded in a journal. It never
removes the stable path. The default grace is the notes' 120-hour walltime plus
a 24-hour margin (`ns-051`); that walltime was not measured for this site and
must be overridden after the site limit is known.

## Dry run

`--dry-run` prints backend selection, probes, and the exact staged EasyBuild
command. It does not invoke EasyBuild, create a journal, move a directory, or
write a module file.

## Residual limits

* `RENAME_EXCHANGE` changes which inode a stable path names. A process that
  opens one file before and another after the swap can still mix generations
  (`ns-054`).
* A process killed with `SIGKILL` during the ordinary-rename fallback cannot
  execute in-process rollback. The next `resume` repairs the journaled state.
* `flock` coherence across two build nodes is not assumed.
* The unsandboxed fallback cannot constrain arbitrary EasyBuild hooks.
* Neither `gc` nor the grace period can see open files on every cluster node.
* A successful transaction is atomic per directory, not across several module
  directories.

## Retiring a copy that moved elsewhere

A package can be installed in this tree and later published somewhere that sits
further along `MODULEPATH`, such as a CVMFS repository. Once that copy is
verified, this tree's module file must stop being what users load, and running
jobs must keep their software directory.

`retire` moves the module file with one `os.replace` to
`<state-root>/retired/<timestamp>/<absolute path without the leading />`, after
fsyncing the file and then both directories. A reader sees the module file or
does not. A job that already loaded it keeps its paths. The stable software
directory is not touched. A rename across filesystems is not atomic, so retire
checks `st_dev` of the module directory against the destination and refuses
when they differ.

The record is a journal under `runs/retire-<timestamp>/` with `kind` set to
`retire`, the module path, the module hash, the software directory parsed from
the module's root assignment, the reason, and the times. It is written as
`retiring` before the rename and `retired` after it, so a crash between the
two leaves a state `rollback` can resolve. A module file without a root
assignment is refused. Retire also refuses a path that is not a regular file,
one already retired, and one whose state root lock is held (`--lock-timeout`
defaults to 0, so it refuses at once).

`rollback retire-<timestamp>` moves the file back with the same primitive and
journals `rolled_back`. It refuses to overwrite a module file that exists again
at the original path.

`gc` starts the grace clock for that software directory at `retired_at`. The
directory is reported as `keep` while a tracked module file still resolves
inside it or the grace period has not elapsed, then as `manual`. Tracked files
are the live modules of publish runs and the original path of any retire that
was rolled back or interrupted. gc does not delete that stable directory. It
deletes only the hidden sibling a directory-swap journal recorded. Deleting the
stable path from a retire record would let one wrong journal entry remove a
directory a job outside this tool still has open.

## `open-004` recommendations

1. **Bubblewrap:** run `bwrap --dev-bind / / true` as the CI account. Use the
   bwrap backend only when it succeeds; otherwise use the verified userns
   backend.
2. **User namespaces:** keep `user.max_user_namespaces` non-zero for the
   dedicated build account under a documented exception. If policy forbids
   that, use a supported setuid bubblewrap deployment or accept the explicitly
   unsandboxed install-path fallback.
3. **Directory exchange:** run `eb-staged-rebuild probe --path <software>` on
   the actual shared filesystem. Require exchange for prefixes where a
   transient missing name is unacceptable; otherwise use the restoring
   fallback during a frozen release window.
4. **Cross-node lock:** contend for the same lock from two build nodes. If both
   acquire it, use a scheduler singleton or another measured distributed lock
   before deployment.
5. **EasyBuild:** pin the pipeline to 5.4.2 or a newer version validated by the
   integration test. Do not rely on the inferred 5.3.0/5.3.1 JSON layout when
   a pinned, tested release is available.

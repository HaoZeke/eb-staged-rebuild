# Design

EasyBuild `--rebuild` and `--force` empty an existing install before the new
install has passed its sanity checks. This program never points EasyBuild at
the live install for a staged build. The program writes a complete replacement
in a separate tree, takes one `flock` for the build and publish transaction,
and then swaps the staged directory with `software/<name>/<version>`.

The swap keeps a real directory at the live install path. A successful
rebuild changes the inode and the file bytes at that path. The retired directory is kept as
`software/<name>/.<version>.<run-id>` for rollback and for running processes
that still hold old files open.

## Build isolation

`--backend auto` selects these backends in order:

1. EasyBuild's experimental bubblewrap backend, after
   `bwrap --dev-bind / / true` succeeds.
2. `unshare` with a user and mount namespace. The helper bind-mounts the live
   software tree and the live module tree read-only. The helper then checks
   that a probe write fails with `EROFS`. The build runs into the stage with
   an install path.
3. The install-path fallback. The fallback still sends EasyBuild to a separate
   software tree and a separate module tree. The fallback has no kernel
   sandbox. The command prints that the run is using this unsandboxed
   fallback. Sites that require sandboxing should select `--backend bwrap` or
   `--backend userns`. Either explicit backend refuses when its probe fails.

The fallback exists for hosts where bubblewrap is absent and unprivileged user
namespaces are disabled. The fallback blocks EasyBuild's normal cleanup of the
live tree. A hook that writes outside its configured install path is outside
that protection.

EasyBuild options that could redirect the build are removed:
`--prefix`, `--installpath*`, `--subdir-*`, `--bwrap*`, and
`--remove-ghost-install-dirs`. `--rebuild` is added unless `--rebuild`,
`--force`, or `-f` is already present.

## Publish path

The planner requires an existing non-symlink live `Name/version` directory for
each module. The planner verifies that the staged install is complete. The
planner writes a run marker into the staged install. The planner prepares a
module file whose root is the stable live path. The planner records and durably
backs up the current module file. The planner moves the stage to a hidden
sibling on the same filesystem.

The publish step attempts Linux `renameat2` with `RENAME_EXCHANGE` first (`1 << 1`,
from `include/uapi/linux/fs.h`). The syscall changes both directory names in
one operation. `EINVAL`, `ENOSYS`, or `EOPNOTSUPP` selects the shared-filesystem
fallback:

1. rename live to `<staged>.fallback-old`;
2. rename staged to live;
3. rename the hold path to staged.

Every caught error restores the original two names, and so does every injected
failure. The error is then raised again. If a process or machine dies between
fallback renames, `resume` reads the journal and the hold name. `resume`
restores the old live name before the next attempt. That recovery does not
make the missing-name window atomic. Only `RENAME_EXCHANGE` does that. A
deployment that requires an uninterrupted name needs a filesystem where the
exchange probe succeeds.

For a multi-module build, all directory swaps happen before module files are
published. If any swap, module write, class-link operation, or journal write
fails, every completed module-file update and directory swap is reversed.
Tests inject a failure before every recorded operation. After each failure,
every live directory still has its original inode and its original bytes.

The module-file replacement is a same-directory `os.replace` after file and
directory `fsync`. The directory swap is atomic for one directory. A build
with more than one module is not one atomic operation. Other users do not
take this tool's advisory lock. Those users can observe a mixed set while a
successful multi-module transaction is in flight.

## Lock, resume, rollback, and collection

A local flock lock on `<state-root>/.lock` is taken first. A POSIX record
lock is then taken on the sidecar `<state-root>/.lock.posix`. The flock lock
excludes threads and processes that share a kernel. The sidecar excludes
other nodes only when the filesystem provides coherent POSIX locks. The
sidecar is opened only while the flock lock is held. The sidecar is closed
before the flock lock is released. Other code in the same process must not
open the sidecar. Closing any descriptor for that inode drops the process's
record locks. A forked child closes inherited descriptors. The child does
not release the parent's locks. Never unlink either file while the state
root is active.

Every node must use this protocol. A flock-only publisher from version 0.2.0
still excludes this process on the same kernel. That publisher does not take
the sidecar. The two versions do not exclude each other across nodes. Drain
flock-only holders before relying on cross-node exclusion. Cross-node
coherence remains an `open-004` measurement. Recovery after a failed node
remains an `open-004` measurement. This repository has not measured General
Parallel File System. The ext4 tests are not acceptance of that filesystem.

After the flock and the POSIX record lock are taken, the journal records
the stage, the stable path, the retired path, the hashes, and the completed
steps. `resume` is idempotent. `resume` first recovers an
interrupted fallback. `resume` then returns any interrupted publish to the
old tree.

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
* This repository has not measured the dual lock from two build nodes. General
  Parallel File System was not measured. The candidate scope is ext4 until a
  site accepts the shared filesystem.
* A module-file replacement keeps the destination's ownership, mode, and
  listed access control list bytes. The known names are the POSIX access
  list, the generic Network File System version 4 list, and the native
  General Parallel File System list. The bytes must read back unchanged.
  This repository does not claim that an unmeasured filesystem preserves them.
* Optional `--ledger-config` appends eb-stack ledger records. The record
  `location` is the stable directory. The journal schema for that run is 4.
  Schema 1 and schema 3 are generation-pointer journals and are refused.
  Schema 2 remains a directory swap without a ledger.
* The unsandboxed fallback cannot constrain arbitrary EasyBuild hooks.
* Neither `gc` nor the grace period can see open files on every cluster node.
* A successful transaction is atomic for one directory. A transaction that
  covers more than one module directory is not one atomic operation.

## Retiring a copy that moved elsewhere

A package can be installed in this tree and later published somewhere that sits
further along `MODULEPATH`, such as a CernVM File System repository. Once that copy is
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

## Baseline divergences

The audit of unmodified `8e7b6d66` listed 11 differences from the staged-rebuild
notes and 22 differences between that tree's own documents and its code. Each
one is closed in this tree, or kept on purpose.

### Staged-rebuild notes

1. Closed. Publish exchanges `software/<name>/<version>` with the staged tree.
2. Closed. Publish and rollback call `renameat2(RENAME_EXCHANGE)` when the probe succeeds.
3. Closed. `EINVAL`, `ENOSYS`, and `EOPNOTSUPP` select the restoring three-rename fallback.
4. Closed. Rollback puts the previous software tree back at the stable path. A retire record is only a module file, and rollback puts that file back.
5. Closed. `--backend auto` is the default.
6. Closed. The order is bubblewrap, then the user namespace, then installpath.
7. Closed. Tests inject exchange failures, each ordinary-rename step, and restoration of a failed swap.
8. Closed. A successful publish changes the live directory inode. Tests require the new bytes at that path.
9. Closed. A caught failure reverses every completed swap and module-file update in that transaction.
10. Closed. Each `open-004` measurement has a recommended answer above.
11. Closed. `CHANGELOG.md` and the Org-mode how-to and reference describe the swap.

### Repository documents at `8e7b6d66`

1. Closed. Dependency order reads Lua `load` and `depends_on`, and Tcl `module load`. A cycle still finishes in path order.
2. Closed. The live-change record includes a SHA-256 of each file's contents.
3. Closed. `--skip-live-check` is documented. It skips that record.
4. Closed. The tool refuses a `--module` value or a parsed module path that leaves the software or module root.
5. Closed. Symlink components of a module path are refused. The refusal still applies when the module file is missing.
6. Closed. Stage roots taken from `bwrap_info.json` must stay inside the run's generation directory.
7. Closed. A missing `Name/version` is refused before EasyBuild. The build does not create the live directory.
8. Closed. The temporary file for an atomic write is created with `mkstemp`, so the name is not a fixed hard-link target.
9. Closed. The state root is refused when any existing component is a symlink.
10. Closed. An `OSError` from the command line is printed as `eb-staged-rebuild:` and exits 2, without a traceback.
11. Closed. The unit suite passes with `ResourceWarning` promoted to an error.
12. Intended. The module preflight can run before a software-symlink refusal. The install build does not. A module-file symlink is refused in planning, after the build and before any swap, because the path may be known only from that preflight.
13. Intended. Bubblewrap is accepted when `bwrap --dev-bind / / true` succeeds. `user.max_user_namespaces` is reported. A zero value does not by itself refuse a setuid bubblewrap that passes the probe.
14. Closed. The runbook states that the preflight runs before the lock. The runbook also states that EasyBuild may run twice. The lock covers build, publish, resume, rollback, retire, and garbage collection.
15. Intended. Dry-run checks the software directory named by `--module`. It does not check the live module file. That file is checked when a real run plans the publish.
16. Closed. `--run-id`, `--lock-timeout`, `--skip-live-check`, `--eb`, `EB_STAGED_REBUILD_EB`, `EB_STAGED_REBUILD_STATE_ROOT`, and `EB_STAGED_REBUILD_GRACE_HOURS` are documented.
17. Closed for the collector: `gc` keeps the state-root lock until collection finishes. Intended for the rest: two state roots are two locks, even when both runs name the same live trees. One state root per live tree is the deployment rule.
18. Closed. The explanation of `atomic_write` is its docstring.
19. Intended. One unreadable journal makes `status` stop. A partial list would hide the broken run.
20. Intended. A crash during planning can leave prepared module files while the journal still says `building`. `resume` from `building` rebuilds that generation.
21. Intended. `resolve` calls a directory complete when it contains install metadata, including the run marker written before publish. The marker means the tree is complete. The marker does not mean the tree is already the live path.
22. Closed. With integration enabled, missing `eb` or Lmod fails the test. The bubblewrap case skips when that probe fails, and the README says so.

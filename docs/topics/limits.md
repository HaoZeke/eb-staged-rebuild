# Limits and open measurements

These are the limits the code and the design note record. The recommendations are the ones in `DESIGN.md`, included as {doc}`../reference/design`.

## What a successful run guarantees

* `RENAME_EXCHANGE` changes which inode a stable path names. A process that opens one file before the swap and another after it can still mix generations.
* A process killed with `SIGKILL` during the ordinary-rename fallback cannot roll back in process. The next `resume` repairs the journaled names. The missing-name window is part of that fallback.
* A transaction that covers more than one module directory is a sequence of exchanges and replaces. It is one atomic directory operation per module directory.
* The unsandboxed install-path fallback cannot constrain an EasyBuild hook that writes outside the generation.
* `gc` and the grace period cannot see open files on every cluster node.
* A module-file replacement keeps ownership, mode, and the listed ACL bytes when those bytes read back unchanged. The known names are the POSIX access list, the generic NFSv4 list, and the native General Parallel File System list. The recorded checks are on ext4.

## `open-004`

The dual lock from two build nodes is unmeasured. General Parallel File System is unmeasured. Recorded checks use ext4.

Before enabling the wrapper on a shared filesystem:

1. Run `bwrap --dev-bind / / true` as the account that will build. Use the bwrap backend when it succeeds; otherwise use the userns backend when that probe succeeds.
2. Keep `user.max_user_namespaces` non-zero for the build account when the userns backend is required. A zero value does not by itself refuse a setuid bubblewrap that passes `bwrap --dev-bind / / true`.
3. Run `eb-staged-rebuild probe --path <software>` on the shared software filesystem. Require exchange for prefixes where a transient missing name is unacceptable. Otherwise the restoring fallback belongs in a frozen release window.
4. Contend for the same lock from two build nodes. If both acquire it, use a scheduler singleton or another measured distributed lock before deployment.
5. Pin the pipeline to EasyBuild 5.4.2 or a newer version the integration test has run.

The walk-through host answered (1) with `bwrap is not installed`, (2) with `user.max_user_namespaces: 192727` and a usable userns sandbox, and (3) with `RENAME_EXCHANGE: yes on ext4`. Items (4) and (5) were not run on that host. The integration test in `tests/test_easybuild_integration.py` is the EasyBuild 5.4 check, and it runs when `EB_STAGED_REBUILD_INTEGRATION=1`.

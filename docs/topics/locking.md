# Locking

A local `flock` on `<state-root>/.lock` is taken first. A POSIX record lock is then taken on `<state-root>/.lock.posix`. The flock excludes threads and processes that share a kernel. The sidecar excludes other nodes only when the filesystem provides coherent POSIX locks.

The sidecar is opened only while the flock is held, and it is closed before the flock is released. Other code in the same process must not open the sidecar. Closing any descriptor for that inode drops the process's record locks. A forked child closes inherited descriptors and does not release the parent's locks. Neither file is unlinked while the state root is in use.

The lock covers build, publish, resume, rollback, retire, and garbage collection. The module preflight `eb --dry-run` runs before the lock is taken. `status`, `probe`, and `resolve` do not take it. `status` does not accept `--lock-timeout`.

A file left behind after a crash is not itself a held lock. The kernel releases the locks when the process exits. The next acquirer truncates `.lock` and writes its pid.

Every node must use this protocol. A 0.2.0 flock-only publisher still excludes this process on the same kernel. That publisher does not take the sidecar, and the two versions do not exclude each other across nodes. Drain flock-only holders before relying on cross-node exclusion.

Two state roots are two locks, even when both runs name the same live trees. One state root per live tree is the deployment rule. `gc` holds the lock until collection finishes.

While a process held `/tmp/ebsr-docs-prefix/.eb-staged-rebuild/.lock`, `gc --lock-timeout 0.2` exited 2:

```text
eb-staged-rebuild: another eb-staged-rebuild holds the lock at /tmp/ebsr-docs-prefix/.eb-staged-rebuild/.lock
```

Cross-node coherence was not measured in this repository. That measurement is `open-004` in {doc}`limits`.

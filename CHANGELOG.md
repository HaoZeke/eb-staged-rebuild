# Changelog

## 0.3.0

* Pair the local flock lock with a POSIX record lock on `.lock.posix`.
  Cross-node coherence is still unmeasured. A 0.2.0 flock-only process does
  not take the sidecar.
* Module-file replacement keeps ownership, mode, and the listed access
  control list bytes. A stale privileged temporary file is refused.
* Drain a build's process group before the writer lock is released.
* Refuse garbage collection when a tracked module cannot be read, and keep a
  hidden sibling that a module reaches through a symlink. Removal stays on
  the sibling's filesystem and owned directories.
* Record a directory-swap publication in an eb-stack ledger outbox. The
  record's `location` is the stable install directory. The journal schema is
  4 when a ledger policy is frozen, and schema 2 otherwise. Schema 1 and
  schema 3 generation-pointer journals are refused.
* This version creates no git tag.
* Recorded checks use ext4.
* Cross-node locking remains `open-004`.
* Behavior on the general parallel file system remains `open-004`.

## 0.2.0

* Stage rebuilds with automatic bubblewrap, user/mount-namespace, and
  documented unsandboxed install-path fallback selection.
* Publish complete trees with `renameat2(RENAME_EXCHANGE)` and a restoring
  ordinary-rename fallback for unsupported filesystems.
* Keep one `flock` across build and publish; add transactional rollback and
  interrupted-fallback recovery.
* Make dry-run report sandbox and swap decisions without invoking EasyBuild.
* Add exhaustive failure injection for build, journal, swap, module, link, and
  rollback paths.
* Bump the journal schema to 2; version 1 generation-pointer runs are refused
  instead of being interpreted as directory-swap transactions.
* Name retention and kernel constants and record their sources.
* Add Org-mode how-to, reference, baseline design audit, and `open-004`
  recommendations.

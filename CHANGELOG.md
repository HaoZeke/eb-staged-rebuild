# Changelog

## 0.3.0

* Pair the local `flock` gate with a POSIX record lock on `.lock.posix`.
  Cross-node coherence is still unmeasured. A 0.2.0 flock-only process does
  not take the sidecar.
* Preserve ownership, mode, and listed ACL bytes when a module file is
  replaced, and refuse a stale privileged temporary file.
* Drain a build's process group before the writer lock is released.
* Refuse garbage collection when a tracked module cannot be read, and keep a
  hidden sibling that a module reaches through a symlink. Removal stays on
  the sibling's filesystem and owned directories.
* Record a directory-swap publication in an eb-stack ledger outbox. The
  record's `location` is the stable install directory. The journal schema is
  4 when a ledger policy is frozen, and schema 2 otherwise. Schema 1 and
  schema 3 generation-pointer journals are refused.
* No git tag is created for this version.

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

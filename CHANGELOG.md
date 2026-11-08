# Changelog

## 0.4.2

* The ledger tests and the pinned-ledger CI action build eb-stack 0.7.0
  (defb6ae); the ledger CLI's receipt, tile-log and checkpoint lines are
  unchanged from 0.6.2.

## 0.4.1

* `ledger_maintenance.anchor` accepts the `index checkpoint` line eb-stack
  0.6.0 prints after the head's checkpoint once the tile log has sealed a
  run (every 256 records), instead of refusing the receipt.

## 0.4.0

* `eb_staged_rebuild.build_events` records `started`, `finished` and
  `aborted` ledger events around one build command through a durable
  outbox, and `eb_staged_rebuild.ledger_maintenance` backfills observed
  history, publishes daily signed anchors, proves the anchor chain and
  projects package history from a verified log (`docs/build-events.md`,
  `docs/ledger-maintenance.md`).
* The ledger adapter and the maintenance verifier read the eb-stack 0.5.2
  CLI: the receipt is the first line `ledger append`, `ledger head` and
  `ledger verify` print, exactly one tile-log or checkpoint line for the
  same size and root follows it (none for the size-0 head of an empty
  log), and a tile log is written beside every private ledger copy before
  `ledger verify` audits it.
* `_bounded` runs the ledger CLI through pipes and kills its process group
  on every exit, including a signal to the caller, so an interrupted
  delivery leaves no eb-stack process behind.

## 0.3.1

* EasyBuild 5.4.1 is in the bwrap key table: its `bwrap_info.json` carries
  `bwrap_installpath_software` and `bwrap_installpath_modules`
  (easybuild-framework-v5.4.1, `easybuild/tools/bwrap.py`), so `probe` on a
  5.4.1 whose Python the probe cannot import reports the keys as read from
  source instead of unknown.

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

# Changelog

## 1.0.0

* A static Zig build of every command, one binary per architecture
  (x86_64, aarch64) built for `linux-musl` on zig-common 0.8.0: `probe`,
  `status`, `resolve`, `retire`, `gc`, `run`, `resume` and `rollback`, with
  and without `--ledger-config`, plus `ledger-maintenance` and
  `build-events` as subcommands. Its output matches the Python tool byte for
  byte except where `COMPAT.md` lists a deliberate difference. Releases carry
  the binaries, `SHA256SUMS` and build provenance attestations.
* CI checks both builds against each other. A differential runs every
  command and compares the trees and journals it leaves. A crash matrix kills
  a run, a rollback and a retire at every fault step (295 cases). A fail
  matrix gives every step an I/O error (152 cases). The signal, ledger,
  metadata, access-control, build-events and real-EasyBuild suites also run
  against the binary. `TESTMAP.md` maps each test that imports the Python
  code to what covers it for the binary, and CI fails on a missing row.
* Fix: a run killed right after `RENAME_EXCHANGE` resumes to the published
  state, and a rollback killed right after its exchange rolls back once.
* Fix (binary): an allocation failure stops the program. It never runs
  `eb` with the parent environment and without the staged install paths.
  A restore that cannot be recorded is not ignored. A damaged build-events
  state file is refused instead of crashing. Error text names the errno the
  kernel gave.
* Fix (binary): a missing `eb-stack` is "cannot start", as in Python, and a
  stop during ledger delivery kills the ledger's process group first.
* The ledger call keeps the first 64 KiB of each stream, returns when
  `eb-stack` exits and stops any process it left behind.
* Documentation: <https://stackhands.github.io/eb-staged-rebuild/>, with the
  release-binary install and the Zig build's differences.

## 0.4.4

* Restore the required access control list before removing inherited
  virtual ACL views, retaining the selected publication policy.
* Accept an unlisted NFSv4-family EINVAL probe only when the same inspection
  lists a readable POSIX access ACL. Listed, denied and POSIX failures
  remain errors.
* Keep the command's reported version consistent with the distribution
  metadata.

## 0.4.3

* From the `fix/cluster-record-lock` line, on the directory-swap model: a
  publication-preparation program that leaves a staged tree without build
  evidence blocks the swap; an empty or relative `--prepare` is refused
  before anything runs; a sealed (read-only) install takes the run marker
  and moves beside the live tree through a writable window on its top
  directory; a read-only module stage is cleared through the owned-tree
  cleanup on resume; the preparation context names each staged tree as
  `generation` too; a prepared access policy is published and rolled back
  even when the module bytes did not change. The branch's prepare,
  publication-metadata, read-only, sealed-install, ledger-policy and
  bounded-output tests come along, written for the swap model; its
  generation-pointer gc and outbox suites stay on the branch.

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
  projects package history from a verified log (`docs/howto/build-events.md`,
  `docs/howto/ledger.md`).
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

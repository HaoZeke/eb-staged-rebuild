# Runbook

Use this wrapper only for an existing real-directory install. New installs do
not need an in-place rebuild and are refused.

## Probe checks

Run `eb-staged-rebuild probe` as the same account on the CI filesystem:

```bash
eb-staged-rebuild probe --path /shared/software
```

Interpret it locally:

* `bwrap: usable` permits `--backend bwrap`.
* `user-namespace sandbox: usable` means `unshare` created a user/mount
  namespace and a test bind mount rejected writes with `EROFS`.
* `RENAME_EXCHANGE: yes` applies only to the probed filesystem.
* The probe prints the lock line below. Cross-node coherence stays `open-004`
  until two build nodes are measured. The sidecar is `<state-root>/.lock.posix`.
  A flock-only 0.2.0 publisher does not take the sidecar. Do not unlink either
  lock file.

```text
lock: local flock lock plus POSIX record lock; cross-node coherence unmeasured (open-004)
```
* Pin EasyBuild to the version shown by the probe; 5.4.2 is the tested target.

Put `--state-root` beside the software tree so moving the completed stage to a
hidden sibling cannot fail with `EXDEV`.

## Dry run

```bash
eb-staged-rebuild run \
  --software /shared/software \
  --modules /shared/modules \
  --module Name/version \
  --dry-run \
  -- Name-version-toolchain.eb --robot
```

Dry-run prints the selected sandbox and staged EasyBuild command. It does not
invoke EasyBuild or create a journal. It refuses a missing or symlinked
`Name/version` when `--module` names it.

## Sandbox policy

`--backend auto` tries bwrap, then the userns sandbox, then the install-path
fallback. The fallback still stages away from the live prefix, but arbitrary
hooks are not kernel-confined. Production policy should use an explicit
`--backend bwrap` or `--backend userns` if that downgrade is unacceptable.
Explicit sandbox backends refuse when their capability probe fails.

## Run

```bash
eb-staged-rebuild run \
  --software /shared/software \
  --modules /shared/modules \
  --backend auto \
  -- Name-version-toolchain.eb --robot
```

The lock is `<state-root>/.lock` and remains held through build and publish.
The module preflight `eb --dry-run` runs before that lock is taken, so a normal
run can invoke EasyBuild twice. The file remaining after a crash is not itself
a held lock.

On success:

* `/shared/software/Name/version` is the new complete directory;
* `/shared/software/Name/.version.<run-id>` is the previous directory;
* the module file still names `/shared/software/Name/version`; and
* the journal records whether exchange or the ordinary-rename fallback ran.

## Failure and resume

```bash
eb-staged-rebuild status --state-root /shared/.eb-staged-rebuild
eb-staged-rebuild resume \
  --state-root /shared/.eb-staged-rebuild \
  RUN_ID
```

Caught build, planning, swap, module, link, and journal failures leave every
live directory at its original inode and contents. `resume` repeats incomplete
work.

The ordinary-rename fallback can be interrupted. Its hold name ends in
`.fallback-old`. A `SIGKILL` or a machine loss can stop between the renames.
The next `resume` recognises either interrupted shape. `resume` restores the
old live name first. Leave those paths for `resume`.

If `RENAME_EXCHANGE` returned an errno other than `EINVAL`, `ENOSYS`, or
`EOPNOTSUPP`, the operation stops without selecting the fallback. Diagnose the
filesystem or permission error.

## Rollback

```bash
eb-staged-rebuild rollback \
  --state-root /shared/.eb-staged-rebuild \
  RUN_ID
```

Rollback puts the hidden previous directory back on the stable path, module by module in reverse order. Rollback then restores the module files. If rollback fails partway, the tool returns every completed operation to the published state. Retrying the same command is then safe.

## Garbage collection

Set the grace from the site's longest job. The repository default is the
notes' unverified 120 hours plus 24 hours.

```bash
eb-staged-rebuild gc \
  --state-root /shared/.eb-staged-rebuild \
  --grace-hours 144 \
  --dry-run
```

Grace still bounds removal. The dry-run lists candidate paths. Read those paths. Run the same command again without `--dry-run` when those paths are the ones to remove. Collection accepts only the exact hidden sibling recorded beside its stable live directory. Collection does not
use node-local `lsof`. Collection cannot see an old file that is open on
another node. A software directory named by `retire` is reported. Collection
leaves that directory in place.

## Retire a module published elsewhere

When the same package is verified on a later `MODULEPATH` entry, move this
tree's module file aside. The stable software directory stays where it is.

```bash
eb-staged-rebuild retire \
  --state-root /shared/.eb-staged-rebuild \
  --module-file /shared/modules/all/Name/version.lua \
  --reason "published on software.surf.nl"
```

`--lock-timeout` defaults to 0. `rollback` with the printed
`retire-<timestamp>` id puts the module file back. The state root and the
module tree must be the same filesystem.

## Command reference

`eb-staged-rebuild --version` prints the version. `eb-staged-rebuild --help`
lists the subcommands `probe`, `run`, `resume`, `rollback`, `gc`, `retire`,
`status`, and `resolve`.

```bash
eb-staged-rebuild probe --path /shared/software --eb eb --json
```

`probe` reports bubblewrap, the user-namespace sandbox, `RENAME_EXCHANGE`, and
EasyBuild. `--eb` is the EasyBuild executable. The environment variable
`EB_STAGED_REBUILD_EB` is the default (`eb` when unset). `--json` prints the
same report as one JSON object.

```bash
eb-staged-rebuild run \
  --software /shared/software \
  --modules /shared/modules \
  --state-root /shared/.eb-staged-rebuild \
  --eb eb \
  --backend auto \
  --grace-hours 144 \
  --dry-run \
  --module Name/version \
  --run-id RUNID \
  --lock-timeout 30 \
  --skip-live-check \
  --prepare /shared/bin/finalize-install \
  --ledger-config /shared/.eb-staged-rebuild/ledger.json \
  -- Name-version-toolchain.eb
```

`run` accepts those options, then the easyconfig after `--`.

* `--software` and `--modules` default to `EASYBUILD_INSTALLPATH_SOFTWARE` and
  `EASYBUILD_INSTALLPATH_MODULES`.
* `--state-root` defaults to `EB_STAGED_REBUILD_STATE_ROOT`, or else a hidden
  sibling of the software tree named `.eb-staged-rebuild`.
* `--backend` is `auto`, `bwrap`, `userns`, or `installpath`.
* `--grace-hours` defaults to `EB_STAGED_REBUILD_GRACE_HOURS`, or else 144.
* The `--run-id` option sets the journal id.
* Without `--run-id`, the id joins the process id to a clock reading in coordinated universal time.
* `--lock-timeout` takes a number of seconds. The command waits that long for
  the state-root lock and then refuses. Omit the option and the wait has no
  deadline.
* `--module` names one `Name/version` directory to check before the build.
  Repeat the option for each known install.
* `--skip-live-check` does not record file contents before the build and does
  not refuse when those contents change before publish.
* `--prepare` names a program by an absolute path; an empty or relative
  value is refused before anything runs. The program gets one argument, a
  JSON context with the run id, the stable and staged paths, and one entry
  per module (`rel`, `staged`, also spelled `generation`, `live_software`,
  `retired`, `live_module`, `prepared`). It runs after the build and before
  the swap. A non-zero exit is the command's exit, with nothing swapped.
  A mode or ACL it sets on a module's `prepared` file is published with
  the module even when the module text did not change, and rollback puts
  the previous policy back. A staged tree it leaves without build evidence
  (`easybuild/*.eb`) blocks the whole publication.
* `--ledger-config` names a JSON eb-stack ledger configuration. `started`
  records are acknowledged before the swap and `finished` after it; a
  required ledger that cannot acknowledge leaves the live tree alone and
  exits 75, and the policy the run started with is frozen in its journal.
* A read-only install, as EasyBuild's `read-only-installdir` leaves it,
  publishes and rolls back with its files and permissions intact; the tool
  opens a writable window on the top directory alone, for the run marker
  and for the move beside the live tree.

```bash
eb-staged-rebuild resume \
  --state-root /shared/.eb-staged-rebuild \
  --lock-timeout 30 \
  RUNID
```

`resume` continues one journal. A retire record is refused. Use `rollback` for
that id.

```bash
eb-staged-rebuild rollback \
  --state-root /shared/.eb-staged-rebuild \
  --lock-timeout 30 \
  RUNID
```

`rollback` swaps a published tree back, or puts a retired module file back.

```bash
eb-staged-rebuild gc \
  --state-root /shared/.eb-staged-rebuild \
  --grace-hours 144 \
  --lock-timeout 30 \
  --dry-run \
  --include-failed
```

`gc` removes a hidden sibling once the grace period has passed.
`--include-failed` also removes the hidden sibling recorded for a failed run
whose modules are still pending, once the grace period has passed. `gc` leaves
the stable software directory named by `retire` in place.

```bash
eb-staged-rebuild retire \
  --state-root /shared/.eb-staged-rebuild \
  --module-file /shared/modules/all/Name/version.lua \
  --reason "published on software.surf.nl" \
  --dry-run \
  --lock-timeout 0
```

`retire` moves one module file aside. `--lock-timeout` defaults to 0.

```bash
eb-staged-rebuild status --state-root /shared/.eb-staged-rebuild
eb-staged-rebuild status --state-root /shared/.eb-staged-rebuild RUNID
```

`status` prints journals. A retire journal includes the software directory it
left in place.

```bash
eb-staged-rebuild resolve /shared/modules/all/Name/version.lua
```

`resolve` prints JSON for one module file: its syntax, the install path it
names, and whether that directory looks complete. The exit status is 1 when
the path is not a complete install.

## Measurements still open (`open-004`)

Before enabling the wrapper:

1. probe bwrap as the CI account;
2. record `user.max_user_namespaces` and the actual userns probe result;
3. probe `RENAME_EXCHANGE` on the shared software filesystem;
4. hold and contend for the same lock from two build nodes; and
5. pin and integration-test one EasyBuild release, preferably 5.4.2 or newer.

If exchange is unavailable and a transient missing name is unacceptable, do
not use the ordinary-rename fallback. Change filesystem/deployment strategy or
schedule a release freeze.

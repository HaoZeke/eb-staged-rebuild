# Runbook

Use this wrapper only for an existing real-directory install. New installs do
not need an in-place rebuild and are refused.

## Preflight

Run the probe as the same account and on the same filesystem used by CI:

```bash
eb-staged-rebuild probe --path /shared/software
```

Interpret it locally:

* `bwrap: usable` permits `--backend bwrap`.
* `user-namespace sandbox: usable` means `unshare` created a user/mount
  namespace and a test bind mount rejected writes with `EROFS`.
* `RENAME_EXCHANGE: yes` applies only to the probed filesystem.
* `flock across nodes: unmeasured` is deliberate. Test two actual build nodes.
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

The ordinary-rename fallback has a hold name ending in `.fallback-old`. A
`SIGKILL` or machine loss can stop between its renames. The next `resume`
recognises either interrupted shape and restores the old live name first. Do
not rename those paths by hand.

If `RENAME_EXCHANGE` returned an errno other than `EINVAL`, `ENOSYS`, or
`EOPNOTSUPP`, the operation stops without selecting the fallback. Diagnose the
filesystem or permission error.

## Rollback

```bash
eb-staged-rebuild rollback \
  --state-root /shared/.eb-staged-rebuild \
  RUN_ID
```

Rollback swaps the hidden previous directory back into the stable path in
reverse module order and restores module files. A caught rollback failure
returns every completed operation to the published state, so retrying the same
command is safe.

## Garbage collection

Set the grace from the site's longest possible job, not from this repository's
default. The default is the notes' unverified 120 hours plus 24 hours.

```bash
eb-staged-rebuild gc \
  --state-root /shared/.eb-staged-rebuild \
  --grace-hours 144 \
  --dry-run
```

Review the paths, then omit `--dry-run`. Collection only accepts the exact
hidden sibling recorded beside its stable live directory. It does not use
node-local `lsof`, and it cannot prove that another node has no old file open.
A software directory named by `retire` is reported, not deleted.

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

`probe --path DIR --eb EB --json` reports bubblewrap, the user-namespace
sandbox, `RENAME_EXCHANGE`, and EasyBuild. `--eb` is the EasyBuild executable.
The environment variable `EB_STAGED_REBUILD_EB` is the default (`eb` when
unset). `--json` prints the same report as one JSON object.

`run` accepts `--software`, `--modules`, `--state-root`, `--eb`, `--backend`,
`--grace-hours`, `--dry-run`, `--module`, `--run-id`, `--lock-timeout`, and
`--skip-live-check`, then the easyconfig after `--`.

* `--software` and `--modules` default to `EASYBUILD_INSTALLPATH_SOFTWARE` and
  `EASYBUILD_INSTALLPATH_MODULES`.
* `--state-root` defaults to `EB_STAGED_REBUILD_STATE_ROOT`, or else a hidden
  sibling of the software tree named `.eb-staged-rebuild`.
* `--backend` is `auto`, `bwrap`, `userns`, or `installpath`.
* `--grace-hours` defaults to `EB_STAGED_REBUILD_GRACE_HOURS`, or else 144.
* `--run-id` forces the journal id. The default is a UTC stamp plus the pid.
* `--lock-timeout SECONDS` waits that long for the state-root lock and then
  refuses. Omit it and the wait has no deadline.
* `--module NAME/VERSION` checks that live directory before the build. Repeat
  it for each known install.
* `--skip-live-check` does not record file contents before the build and does
  not refuse when those contents change before publish.

`resume --state-root DIR --lock-timeout SECONDS RUN_ID` continues one journal.
A retire record is refused; use `rollback` for that id.

`rollback --state-root DIR --lock-timeout SECONDS RUN_ID` swaps a published
tree back, or puts a retired module file back.

`gc --state-root DIR --grace-hours HOURS --lock-timeout SECONDS --dry-run
--include-failed` removes a hidden sibling once the grace period has passed.
`--include-failed` also removes the hidden sibling recorded for a failed run
whose modules are still pending, once the grace period has passed. It does not
delete the stable software directory named by `retire`.

`retire --state-root DIR --module-file PATH --reason TEXT --dry-run
--lock-timeout SECONDS` moves one module file aside. `--lock-timeout` defaults
to 0.

`status --state-root DIR [RUN_ID]` prints journals. A retire journal includes
the software directory it left in place.

`resolve MODULE` prints JSON for one module file: its syntax, the install path
it names, and whether that directory looks complete. The exit status is 1 when
the path is not a complete install.

## Site gates (`open-004`)

Before enabling the wrapper:

1. probe bwrap as the CI account;
2. record `user.max_user_namespaces` and the actual userns probe result;
3. probe `RENAME_EXCHANGE` on the shared software filesystem;
4. hold and contend for the same lock from two build nodes; and
5. pin and integration-test one EasyBuild release, preferably 5.4.2 or newer.

If exchange is unavailable and a transient missing name is unacceptable, do
not use the ordinary-rename fallback. Change filesystem/deployment strategy or
schedule a release freeze.

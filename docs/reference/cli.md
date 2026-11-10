# Command line

`eb-staged-rebuild --help` lists the subcommands. `eb-staged-rebuild --version` prints the version from `eb_staged_rebuild.__version__` (0.4.1 in this tree). `--help` is accepted on the main parser and on every subcommand.

```text
usage: eb-staged-rebuild [-h] [--version]
                         {probe,run,resume,rollback,gc,retire,status,resolve}
                         ...
```

Exit status 0 is success. Exit status 2 is a refusal, `NeedsIntervention`, another `StagedRebuildError`, or an `OSError` raised out of the command. A build that exits non-zero returns that status. A preparation program's non-zero status is returned as-is. Exit status 75 means a required ledger event is still pending.

## probe

Report bubblewrap, user namespaces, the user-namespace sandbox, `RENAME_EXCHANGE`, and EasyBuild.

```text
eb-staged-rebuild probe [-h] [--path PATH] [--eb EB] [--json]
```

`--path` is the directory whose filesystem is probed (default `.`). `--eb` is the EasyBuild executable (default `EB_STAGED_REBUILD_EB`, or `eb`). `--json` prints one JSON object. The keys on the walk-through host were `bwrap`, `easybuild`, `filesystem`, `flock`, `rename_exchange`, `user_namespaces`, and `userns_sandbox`.

## run

Build and exchange a staged tree.

```text
eb-staged-rebuild run [-h] [--software SOFTWARE] [--modules MODULES]
                      [--state-root STATE_ROOT] [--eb EB]
                      [--backend {auto,bwrap,userns,installpath}]
                      [--grace-hours GRACE_HOURS] [--dry-run]
                      [--module NAME/VERSION] [--run-id RUN_ID]
                      [--lock-timeout LOCK_TIMEOUT] [--skip-live-check]
                      [--prepare PREPARE] [--ledger-config LEDGER_CONFIG]
                      ...
```

The easyconfig and its options follow `--`. `--software` and `--modules` default to `EASYBUILD_INSTALLPATH_SOFTWARE` and `EASYBUILD_INSTALLPATH_MODULES`. `--state-root` defaults to `EB_STAGED_REBUILD_STATE_ROOT`, or else `<parent of software>/.eb-staged-rebuild`. `--backend` is `auto`, `bwrap`, `userns`, or `installpath`. `--grace-hours` defaults to `EB_STAGED_REBUILD_GRACE_HOURS`, or else 144. `--dry-run` prints the decision and writes nothing. `--module` may be repeated. `--run-id` names the journal. `--lock-timeout` is seconds to wait for the lock; omit it and the wait has no deadline. `--skip-live-check` skips the before/after content record. `--prepare` is an absolute path. `--ledger-config` is the eb-stack JSON configuration.

## resume

Continue one journal. A retire id is refused.

```text
eb-staged-rebuild resume [-h] --state-root STATE_ROOT
                         [--lock-timeout LOCK_TIMEOUT] run_id
```

## rollback

Exchange a published tree back, or put a retired module file back.

```text
eb-staged-rebuild rollback [-h] --state-root STATE_ROOT
                           [--lock-timeout LOCK_TIMEOUT] run_id
```

## gc

Remove hidden retired siblings after the grace period.

```text
eb-staged-rebuild gc [-h] --state-root STATE_ROOT
                     [--grace-hours GRACE_HOURS] [--dry-run]
                     [--lock-timeout LOCK_TIMEOUT] [--include-failed]
```

`--include-failed` also removes the hidden sibling recorded for a failed run whose modules are still pending, once the grace period has passed.

## retire

Move a module file out of the module tree after its package is published elsewhere.

```text
eb-staged-rebuild retire [-h] --state-root STATE_ROOT --module-file MODULE_FILE
                         [--reason REASON] [--dry-run]
                         [--lock-timeout LOCK_TIMEOUT]
```

`--lock-timeout` defaults to 0.

## status

Show run journals. A run id limits the listing to that journal.

```text
eb-staged-rebuild status [-h] --state-root STATE_ROOT [run_id]
```

## resolve

Print the install path a module file names. Exit status 1 when `complete` is false. Exit status 0 when `kind` is `file` and `complete` is true.

```text
eb-staged-rebuild resolve [-h] module
```

## Related command lines

`python3 -m eb_staged_rebuild.build_events` takes `--config`, `--metadata`, and either `--deliver` or a command after `--`. See {doc}`../howto/build-events`.

`python3 -m eb_staged_rebuild.ledger_maintenance` takes `backfill`, `anchor`, `verify-anchors`, or `project`. See {doc}`../howto/ledger`.

# Recover and roll back

`status` reads journals. `resume` continues a build or an interrupted publish. `rollback` puts a published generation back, or puts a retired module file back. `gc` deletes a hidden sibling after the grace period. The commands below were run against `/tmp/ebsr-docs-prefix`.

## Read the journal

```bash
eb-staged-rebuild status --state-root "$STATE"
eb-staged-rebuild status --state-root "$STATE" demo-run
```

After the failed build, before resume:

```text
demo-run  failed  backend=installpath  modules=0/0
  eb exited 3; live modules were not changed
```

The live `PAYLOAD` was still `payload-v1`. A failed build that exits before planning has an empty module plan, so the count is `0/0`.

`status` does not take the state-root lock and does not accept `--lock-timeout`. One unreadable journal stops the listing.

## Resume a failed build

```bash
eb-staged-rebuild resume --state-root "$STATE" demo-run
```

```text
eb-staged-rebuild: resume demo-run from failed: rebuilding the unpublished generation
eb-staged-rebuild: swapped all/demo/1.lua (renameat2-exchange)
eb-staged-rebuild: published module all/demo/1.lua
eb-staged-rebuild: run demo-run published 1 module(s)
```

`resume` from `created`, `building`, or `failed` rebuilds the generation when nothing has been published. `resume` from `built` or `publishing` publishes the generation that is already recorded. A complete run is a no-op. A rolled-back run is refused: start a new run id. A retire id is refused:

```text
eb-staged-rebuild: run retire-20261110T130126Z is a retire record; rollback restores the module file
```

That command exited 2.

## Interrupted ordinary-rename fallback

When `renameat2(RENAME_EXCHANGE)` returns `EINVAL`, `ENOSYS`, or `EOPNOTSUPP`, publication uses three renames. The hold name is the staged path plus `.fallback-old`. Any other errno from the exchange leaves both names as they were and does not select the fallback.

A caught error during the three renames restores the original two names before it is raised. A `SIGKILL` or a machine loss can stop between those renames. The next `resume` calls `recover_interrupted_swap`, which recognises the two interrupted shapes (live missing and the hold present, or live present and the staged name missing) and restores the old live name before another attempt. Leave a path ending in `.fallback-old` for `resume`. That recovery repairs the journaled names. The window where one name is missing is closed only by `RENAME_EXCHANGE`.

## Roll a published generation back

```bash
eb-staged-rebuild rollback --state-root "$STATE" demo-run
```

```text
eb-staged-rebuild: restored all/demo/1.lua
eb-staged-rebuild: run demo-run rolled back; replacement kept at its retired path
```

The live `PAYLOAD` was `payload-v1` again. Status:

```text
demo-run  rolled_back  backend=installpath  modules=0/1
  rolled back
  restored   all/demo/1.lua  live=/tmp/ebsr-docs-prefix/software/demo/1 retired=/tmp/ebsr-docs-prefix/software/demo/.1.demo-run
```

Rollback walks the module plan in reverse. It exchanges each retired directory back onto the stable path, restores each module file from the backup taken at publish, and removes symlinks this run created. A second rollback of the same id prints:

```text
eb-staged-rebuild: run demo-run is already rolled back
```

If rollback fails partway, the tool exchanges back to the published generation and rewrites the published module files, then re-raises the error. The published tree is what a retry of `rollback` starts from.

Only a journal in state `complete` can roll back. The replacement tree stays at the hidden sibling so a later `gc` can remove it.

## Collect hidden siblings

The default grace is 144 hours: the notes' 120-hour walltime plus 24 hours. That walltime was not measured for a deployment site. Set `--grace-hours` from the site's longest job, or set `EB_STAGED_REBUILD_GRACE_HOURS`.

```bash
eb-staged-rebuild gc --state-root "$STATE" --grace-hours 144 --dry-run
```

```text
keep: /tmp/ebsr-docs-prefix/software/demo/.1.demo-run (within grace (rolled-back candidate))
```

`--include-failed` on the same tree printed the same `keep` line. The clock for a rolled-back run starts at `rolled_back_at`. The clock for a complete run starts at `published_at`. A failed run is eligible only with `--include-failed`, and only when a module is still `pending` and the hidden sibling exists.

After the grace period, `--dry-run` lists `remove`. On the same prefix, once a second rolled-back run (`demo-prepare`) was also present:

```bash
eb-staged-rebuild gc \
  --state-root /tmp/ebsr-docs-prefix/.eb-staged-rebuild \
  --grace-hours 0 \
  --dry-run
```

```text
remove: /tmp/ebsr-docs-prefix/software/demo/.1.demo-prepare (rolled-back candidate)
remove: /tmp/ebsr-docs-prefix/software/demo/.1.demo-run (rolled-back candidate)
```

Drop `--dry-run` only after those paths are the ones to delete. Collection deletes the exact hidden sibling recorded beside its stable directory. It uses owned-directory removal on that sibling's filesystem. It does not call `lsof` and cannot see a file that is open on another node. A stable directory named by `retire` is reported as `keep` or `manual` and is left in place. See {doc}`retire`.

## Lock timeout

`run`, `resume`, `rollback`, `gc`, and `retire` take `<state-root>/.lock` and then `<state-root>/.lock.posix`. Omit `--lock-timeout` and the wait has no deadline, except `retire`, whose default is 0 (refuse at once).

While another process held the lock, this command exited 2:

```bash
eb-staged-rebuild gc \
  --state-root /tmp/ebsr-docs-prefix/.eb-staged-rebuild \
  --grace-hours 144 \
  --dry-run \
  --lock-timeout 0.2
```

```text
eb-staged-rebuild: another eb-staged-rebuild holds the lock at /tmp/ebsr-docs-prefix/.eb-staged-rebuild/.lock
```

# Publication and rollback

Publication is one journaled transaction under the state-root lock. The preferred directory operation is `renameat2(RENAME_EXCHANGE)`. Module files follow, each with a same-directory replace.

## Exchange

`swap_directories` recovers an interrupted fallback, checks that the two paths are real directories in the same parent, and then exchanges them. `EINVAL`, `ENOSYS`, and `EOPNOTSUPP` select the fallback. Any other exchange error leaves both names unchanged.

The fallback hold path is `<staged>.fallback-old`:

1. Rename the live directory to the hold path.
2. Rename the staged directory to the live name.
3. Rename the hold path to the staged name.

Every caught error restores the original two names, and so does every injected failure in the test suite. The error is then raised again. `resume` reads the journal and the hold name and restores the old live name before the next attempt. See {doc}`../howto/recover`.

The parent directory is fsynced after a successful exchange. The journal records `swap_method` as `renameat2-exchange` or `rename-fallback`. The recorded `demo-run` journal has `"swap_method": "renameat2-exchange"` and `"rollback_method": "renameat2-exchange"`.

## Module files and links

Before any live name moves, the tool reads each prepared module and its access metadata. A prepared file whose bytes no longer match the plan, or whose ACL bytes cannot be read, blocks the publication.

For each module the tool:

* writes a backup of the previous module file under `runs/<run-id>/backups/` when the live file existed;
* moves the staged tree to the hidden sibling if it is not already there;
* exchanges the sibling with the stable directory;
* replaces the live module file from the prepared bytes, copying owner, mode, and the listed ACL bytes from the prepared file.

The ACL names read and checked are `system.gpfs_nfs4_acl`, `system.nfs4_acl`, and `system.posix_acl_access`. The bytes must read back unchanged. The repository's recorded checks use ext4.

Class links (symlinks EasyBuild wrote in the staged module tree) are created on the live tree when the destination is missing. An existing symlink with the same target is left as present. Anything else at that path is kept.

If any swap, module write, link creation, or journal write fails, completed module-file updates and directory swaps in that transaction are reversed. The journal returns to `built` with the message `publish interrupted; old live tree restored` when that restoration succeeds. When restoration itself fails, the error is `NeedsIntervention`.

## Rollback

`rollback` of a `complete` run exchanges in reverse order, restores module bytes from the backups, and unlinks symlinks this run created. A failure during rollback exchanges back to the published generation. The command can then be retried. A run that is already `rolled_back`, with every module `restored`, prints that fact and exits 0.

The replacement tree remains at the hidden sibling. `gc` may delete it after the grace period. `gc` never deletes the stable path. See {doc}`../howto/recover`.

## Ledger records on the publication

When a ledger policy is frozen, `started` records are appended before the exchange and `finished` records after it. Rollback appends a correction of the `finished` record, or an `aborted` record when publication never finished. The `location` field is the stable directory. Event ids look like `eb-staged-rebuild/<scope>/<run-id>/<module-rel>/<stage>`. A required ledger that leaves events pending exits 75 and does not exchange the live directory.

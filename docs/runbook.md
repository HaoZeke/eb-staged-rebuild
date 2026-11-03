# Runbook

Use this on a prefix where jobs are loading modules. Read [DESIGN.md](../DESIGN.md) for why the live directory is not renamed.

## Before the first rebuild

1. `eb-staged-rebuild probe --path <software-dir>`.
   * `bwrap: usable` is required only for `--backend bwrap`. The default backend is `installpath` and does not run bubblewrap.
   * `user.max_user_namespaces` is the host's sysctl. A value of 0 means the bwrap backend will refuse. This does not answer the site's STIG (`open-004`).
   * `RENAME_EXCHANGE` is reported for the filesystem that holds `--path`. The publish step does not use it. `yes` on one ext4 disk is not a result for GPFS.
   * `flock across nodes: unmeasured` is intentional. Two runs on one machine take the same lock. Two build nodes are `open-004`.
2. `eb --show-config` and confirm `remove-ghost-install-dirs` is not true. This tool strips the command-line flag and the environment variable. One local run with a config file that set it true, and with a fresh module tree, left the old install in place. Do not treat that as a test of every configuration. A generation's `modules/` directory is removed before `eb` runs so a resumed build does not leave EasyBuild a module file that names the live directory.
3. Pick `--grace-hours` from the longest job the site will run. The default 144 is the figure in the notes (120 hours plus a day). It is not a measured walltime. `gc` will not delete anything until that many hours after a later publish or a rollback. It never deletes `software/<name>/<version>`.

## Dry run

```bash
eb-staged-rebuild run \
  --software /software \
  --modules /modules \
  --module Name/version \
  --dry-run \
  -- file.eb --robot
```

Dry-run prints the `eb` command and the probe summary. It does not create a run journal and it does not call `eb`. A symlink at `software/<name>/<version>` makes the dry-run exit 2 when `--module` names it. The bwrap backend also exits 2 when bubblewrap cannot create a namespace.

## Run

```bash
eb-staged-rebuild run \
  --software /software \
  --modules /modules \
  -- file.eb --robot
```

The arguments after `--` are passed to `eb`, except install-path, prefix, bwrap, and `--remove-ghost-install-dirs`. `--rebuild` is added when neither `--rebuild` nor `--force` is present.

`--backend bwrap` runs `eb --experimental --bwrap`. If the probe fails, the run stops. It does not rebuild in place. EasyBuild 5.3.0 and 5.3.1 omit `bwrap_installpath_software`; those two versions use `<bwrap-installpath>/software` and `<bwrap-installpath>/modules`, which is what their `bwrap.py` joins. Any other version that omits the key is left unpublished. An overlay upper (`--overlay` in `bwrap_cmd`) is not a complete tree and is not published.

The lock is `flock` on `<state-root>/.lock`, held from the start of the build until publish finishes. The lock file remaining after a crash is not a held lock. The pid in the file is a hint.

## Crash

`status` shows the journal state.

| State | What `resume` does |
| --- | --- |
| `created`, `building`, `failed` | Runs `eb` again into the same unpublished generation, then publishes. |
| `built`, `publishing` | Publishes the module files that are not yet in place. Does not run `eb`. |
| `complete` | Does nothing. |
| `rolled_back` | Refuses. Start a new run. |

A live module file is either the previous complete file or the new complete file. `resume` is safe to repeat. If it reports that the file matches neither, stop and do not delete directories by hand until the module file has been compared with `runs/<id>/backups/`.

`rollback` copies the backup back with the same same-directory replace, in reverse publish order. The generation directory stays. A module file this run created, and that still matches what was published, is removed. A symlink this run created at a class path is removed. An existing class symlink is left alone, because unlinking it would drop that name.

## After it is published

`resolve` prints the directory the module file names and whether that directory looks complete (`PAYLOAD`, `easybuild/*.eb`, or the stamp this tool writes).

`gc --dry-run` lists generations it would remove. A generation is removed only when all of these hold:

* it is under `<state-root>/generations/`
* no tracked module file resolves inside it
* a later publish recorded the previous root, or the run was rolled back
* the grace period has passed

`--include-failed` also removes failed runs that published nothing and are older than the grace period. Journals are kept.

Do not `gc` a generation that a dependent's `DT_RPATH` or a user script still names. This tool does not search for those. The original `software/<name>/<version>` directory is not a generation under the state root, so `gc` will not remove it.

## Symlinks

If `software/<name>/<version>` is a symlink, the run stops before `eb`. The same is true of the module file that would be replaced. Class symlinks that already point at the real module file are left in place; the file they point at is what changes.

## What still goes wrong

A job that does not reload the module keeps the old generation until it ends. A binary that baked in the old path keeps it until that binary is rebuilt. Two nodes can still publish at once if `flock` does not span them. A crash while several modules are being published leaves a mixture of complete generations, not a half-written file; `resume` or `rollback` is how that mixture is closed.

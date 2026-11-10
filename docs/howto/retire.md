# Retire a module published elsewhere

When the same package is verified on a later `MODULEPATH` entry, `retire` moves this tree's module file out of the module tree. The stable software directory stays where it is. Jobs that already loaded the module keep the paths they have. Readers see the module file, or they do not: the move is one `os.replace` on the same filesystem.

The state root and the module file's directory must be the same device. A different `st_dev` is refused. The file must be a regular file with a `root` assignment. A symlink is refused. `--lock-timeout` defaults to 0.

## Dry run

```bash
eb-staged-rebuild retire \
  --state-root /tmp/ebsr-docs-prefix/.eb-staged-rebuild \
  --module-file /tmp/ebsr-docs-prefix/modules/all/demo/1.lua \
  --reason "published on software.surf.nl" \
  --dry-run
```

```text
eb-staged-rebuild: would retire /tmp/ebsr-docs-prefix/modules/all/demo/1.lua (software directory /tmp/ebsr-docs-prefix/software/demo/1 is left in place)
```

## Move the file

```bash
eb-staged-rebuild retire \
  --state-root /tmp/ebsr-docs-prefix/.eb-staged-rebuild \
  --module-file /tmp/ebsr-docs-prefix/modules/all/demo/1.lua \
  --reason "published on software.surf.nl"
```

```text
eb-staged-rebuild: retired /tmp/ebsr-docs-prefix/modules/all/demo/1.lua -> /tmp/ebsr-docs-prefix/.eb-staged-rebuild/retired/20261110T130126Z/tmp/ebsr-docs-prefix/modules/all/demo/1.lua; software directory /tmp/ebsr-docs-prefix/software/demo/1 left in place
```

The destination is `<state-root>/retired/<timestamp>/<absolute path without the leading slash>`. The file and both directories are fsynced before the rename returns. The journal id is `retire-<timestamp>`. Status prints the software directory it left in place:

```text
retire-20261110T130126Z  retired  retire  /tmp/ebsr-docs-prefix/modules/all/demo/1.lua
  software directory /tmp/ebsr-docs-prefix/software/demo/1
```

The journal is stored as schema 2 with `"kind": "retire"`. It is written as `retiring` before the rename and `retired` after it.

## Put the module file back

`resume` refuses a retire id. `rollback` moves the file back and refuses to overwrite a module file that already exists at the original path.

```bash
eb-staged-rebuild rollback \
  --state-root /tmp/ebsr-docs-prefix/.eb-staged-rebuild \
  retire-20261110T130126Z
```

```text
eb-staged-rebuild: restored /tmp/ebsr-docs-prefix/modules/all/demo/1.lua
eb-staged-rebuild: run retire-20261110T130126Z rolled back; /tmp/ebsr-docs-prefix/modules/all/demo/1.lua restored
```

The module file was present after that command.

## Garbage collection

`gc` starts the grace clock for the software directory at `retired_at`. While a tracked module still resolves inside that directory, or the grace period has not elapsed, the action is `keep`. Afterwards the action is `manual`. `gc` does not delete that stable directory. Deleting it from a retire record would let one journal entry remove a directory a job outside this tool still has open. Tracked module files are the live modules of publish runs and the original path of a retire that was rolled back or interrupted.

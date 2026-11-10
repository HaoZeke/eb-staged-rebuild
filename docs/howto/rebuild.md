# Rebuild and publish

Use this for an existing real-directory install. A missing `software/<name>/<version>` is refused before EasyBuild runs. The state root must be on the same filesystem as the live software tree; a cross-device rename returns `EXDEV` and the tool asks for `--state-root` beside that tree.

The transcript in this page is from `docs/examples/walkthrough.sh` and a follow-up `--prepare` run, both against `/tmp/ebsr-docs-prefix` with `docs/examples/eb-standin` and `--backend installpath`. Substitute the site software root, module root, and `eb` on a cluster. `--backend auto` tries bubblewrap, then the user-namespace sandbox, then the install-path fallback. An explicit `--backend bwrap` or `--backend userns` refuses when its probe fails.

## Probe the filesystem

```bash
eb-staged-rebuild probe --path "$SOFTWARE" --eb "$STANDIN"
```

```text
bwrap: not usable
  reason: bwrap is not installed
user.max_user_namespaces: 192727
user-namespace sandbox: usable
RENAME_EXCHANGE: yes on ext4
EasyBuild framework: unknown
  bwrap stage-path keys: None (unknown)
filesystem: ext4 /
lock: local flock lock plus POSIX record lock; cross-node coherence unmeasured (open-004)
```

`RENAME_EXCHANGE: yes` applies to the filesystem of `--path`. The lock line is printed on every probe. Cross-node coherence of the POSIX sidecar `<state-root>/.lock.posix` is unmeasured (`open-004`). A 0.2.0 flock-only process does not take the sidecar. Leave both lock files in place while the state root is in use. Details are in {doc}`../topics/locking`.

## Dry-run the staged command

```bash
eb-staged-rebuild run \
  --software "$SOFTWARE" \
  --modules "$MODULES" \
  --state-root "$STATE" \
  --eb "$STANDIN" \
  --backend installpath \
  --module demo/1 \
  --dry-run \
  -- demo-1.eb --robot
```

```text
eb-staged-rebuild: dry-run: no build, no module file written
eb-staged-rebuild: backend: installpath
eb-staged-rebuild: publish: renameat2(RENAME_EXCHANGE), with a restoring ordinary-rename fallback
eb-staged-rebuild: command: /agent/repos/eb-staged-rebuild/docs/examples/eb-standin --installpath-software=/tmp/ebsr-docs-prefix/.eb-staged-rebuild/generations/<run-id>/software --installpath-modules=/tmp/ebsr-docs-prefix/.eb-staged-rebuild/generations/<run-id>/modules demo-1.eb --robot --rebuild
```

The full dry-run listing is in the {doc}`../intro/tutorial`. Options that would point EasyBuild at the live tree are removed from the staged command: `--prefix`, `--installpath`, `--installpath-software`, `--installpath-modules`, `--subdir-software`, `--subdir-modules`, `--bwrap`, `--disable-bwrap`, `--bwrap-installpath`, `--bwrap-options`, and `--remove-ghost-install-dirs`.

A dry-run that omits the easyconfig exits 2:

```text
eb-staged-rebuild: pass the easyconfig after --, for example: eb-staged-rebuild run --software S --modules M -- file.eb
```

## Run the rebuild

A real run takes the state-root lock, calls `eb --dry-run` to learn module names (unless a test builder is injected), checks each `Name/version` directory, then builds. The preflight runs before the lock, so EasyBuild can run twice. `--module` names directories to check; names discovered from the dry-run are added to that list.

```bash
eb-staged-rebuild run \
  --software "$SOFTWARE" \
  --modules "$MODULES" \
  --state-root "$STATE" \
  --eb "$STANDIN" \
  --backend installpath \
  --run-id demo-run \
  --module demo/1 \
  -- demo-1.eb --robot
```

On the recorded resume of `demo-run` (the first attempt exited 3 because `EB_STANDIN_FAIL=1`):

```text
eb-staged-rebuild: resume demo-run from failed: rebuilding the unpublished generation
eb-staged-rebuild: running /agent/repos/eb-staged-rebuild/docs/examples/eb-standin --installpath-software=/tmp/ebsr-docs-prefix/.eb-staged-rebuild/generations/demo-run/software --installpath-modules=/tmp/ebsr-docs-prefix/.eb-staged-rebuild/generations/demo-run/modules demo-1.eb --robot --rebuild
eb-staged-rebuild: swapped all/demo/1.lua (renameat2-exchange)
eb-staged-rebuild: published module all/demo/1.lua
eb-staged-rebuild: run demo-run published 1 module(s)
```

```text
demo-run  complete  backend=installpath  modules=1/1
  published
  published  all/demo/1.lua  live=/tmp/ebsr-docs-prefix/software/demo/1 retired=/tmp/ebsr-docs-prefix/software/demo/.1.demo-run
```

The live `PAYLOAD` was `payload-v2`. The module file still contained `local root = "/tmp/ebsr-docs-prefix/software/demo/1"`. The status field `all/demo/1.lua` is the module path relative to the module root. The software directory in this example is `demo/1`, and the hidden sibling is `demo/.1.demo-run`.

Publication order, for every module in the plan:

1. Back up the current module file when one exists.
2. Move the staged install to the hidden sibling.
3. Exchange every sibling with its stable directory.
4. Replace every module file with `os.replace` in that file's directory.
5. Create module symlinks that the staged tree recorded and the live tree does not already have.

A failure during that sequence restores the previous directories and module files. See {doc}`../topics/publication`.

## Set permissions before the exchange

`--prepare` is an absolute path. An empty value is refused before anything runs:

```text
eb-staged-rebuild: --prepare must name a program; it is empty
```

A relative path is refused as soon as the run checks its roots:

```text
eb-staged-rebuild: publication preparation executable must be an absolute path
```

The program receives one argument, the path of `prepare-context.json`. It runs after the build and before any live name is exchanged. A non-zero exit is the command's exit, and the live directory stays as it was. A staged tree left without build evidence (`PAYLOAD`, or `easybuild/*.eb`) blocks the whole publication. A mode or ACL set on a module's `prepared` file is published with the module, including when the module bytes did not change. Rollback writes the previous policy back.

This prepare program copied the context aside and exited 0. The run id was `demo-prepare`:

```bash
eb-staged-rebuild run \
  --software /tmp/ebsr-docs-prefix/software \
  --modules /tmp/ebsr-docs-prefix/modules \
  --state-root /tmp/ebsr-docs-prefix/.eb-staged-rebuild \
  --eb /agent/repos/eb-staged-rebuild/docs/examples/eb-standin \
  --backend installpath \
  --run-id demo-prepare \
  --module demo/1 \
  --prepare /tmp/ebsr-docs-prefix/prepare.sh \
  -- demo-1.eb
```

```text
eb-staged-rebuild: preparing publication permissions: /tmp/ebsr-docs-prefix/prepare.sh /tmp/ebsr-docs-prefix/.eb-staged-rebuild/runs/demo-prepare/prepare-context.json
eb-staged-rebuild: swapped all/demo/1.lua (renameat2-exchange)
eb-staged-rebuild: published module all/demo/1.lua
eb-staged-rebuild: run demo-prepare published 1 module(s)
```

The context file, with keys sorted as the tool writes them:

```json
{
  "generation_root": "/tmp/ebsr-docs-prefix/.eb-staged-rebuild/generations/demo-prepare",
  "live_modules": "/tmp/ebsr-docs-prefix/modules",
  "live_software": "/tmp/ebsr-docs-prefix/software",
  "modules": [
    {
      "generation": "/tmp/ebsr-docs-prefix/.eb-staged-rebuild/generations/demo-prepare/software/demo/1",
      "live_module": "/tmp/ebsr-docs-prefix/modules/all/demo/1.lua",
      "live_software": "/tmp/ebsr-docs-prefix/software/demo/1",
      "prepared": "/tmp/ebsr-docs-prefix/.eb-staged-rebuild/runs/demo-prepare/prepared/all/demo/1.lua",
      "rel": "all/demo/1.lua",
      "retired": "/tmp/ebsr-docs-prefix/software/demo/.1.demo-prepare",
      "staged": "/tmp/ebsr-docs-prefix/.eb-staged-rebuild/generations/demo-prepare/software/demo/1"
    }
  ],
  "publication": "directory-swap",
  "run_id": "demo-prepare",
  "stage_modules": "/tmp/ebsr-docs-prefix/.eb-staged-rebuild/generations/demo-prepare/modules",
  "stage_software": "/tmp/ebsr-docs-prefix/.eb-staged-rebuild/generations/demo-prepare/software",
  "state_root": "/tmp/ebsr-docs-prefix/.eb-staged-rebuild"
}
```

`generation` and `staged` are the same directory: the staged tree, under the name a finalizer written for the generation directory already uses. A read-only install, as EasyBuild's `read-only-installdir` leaves it, is published with its file modes intact. The tool opens a writable window on the top directory for the run stamp and for the rename onto the live filesystem, then restores the mode.

## Record the publication in a ledger

`--ledger-config` names a JSON file. `started` records are acknowledged before the exchange. `finished` records are acknowledged after it. The record's `location` is the stable `software/<name>/<version>` path. A required ledger that cannot acknowledge leaves the live tree alone and the command exits 75. The policy read at the start of the run is frozen in the journal (schema 4). The configuration keys are in {doc}`../reference/environment`.

`python3 -m eb_staged_rebuild.build_events` records a separate build command. It is documented in {doc}`build-events`.

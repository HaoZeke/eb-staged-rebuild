# Tutorial

This is one path from an empty scratch prefix to a published generation and back. It uses `docs/examples/eb-standin`, a program that answers the two requests the tool sends to EasyBuild: a `--dry-run` module line for `demo/1`, and an install under `--installpath-software` and `--installpath-modules`. The same `eb-staged-rebuild` flags apply when `--eb` names the site `eb`.

The commands below are what `docs/examples/walkthrough.sh` runs. The output blocks are from one run of that script with `WALKTHROUGH_ROOT=/tmp/ebsr-docs-prefix` on an ext4 filesystem. `bwrap` was absent, the user-namespace sandbox was usable, and `RENAME_EXCHANGE` succeeded. `user.max_user_namespaces` on that host was 192727.

## Create the live install

The tool rebuilds a real directory that already exists. It refuses to create `software/<name>/<version>`.

```bash
SOFTWARE=/tmp/ebsr-docs-prefix/software
MODULES=/tmp/ebsr-docs-prefix/modules
STATE=/tmp/ebsr-docs-prefix/.eb-staged-rebuild
STANDIN=/path/to/eb-staged-rebuild/docs/examples/eb-standin
chmod +x "$STANDIN"

mkdir -p "$SOFTWARE/demo/1/easybuild" "$MODULES/all/demo"
printf 'payload-v1\n' > "$SOFTWARE/demo/1/PAYLOAD"
printf "name = 'demo'\nversion = '1'\n" > "$SOFTWARE/demo/1/easybuild/demo-1.eb"
cat > "$MODULES/all/demo/1.lua" <<EOF
local root = "$SOFTWARE/demo/1"
conflict("demo")
setenv("EBROOTDEMO", root)
setenv("EBVERSIONDEMO", "1")
EOF
```

The module file names the stable path. After publication it still names that path; the directory inode at the path changes. The staging model is {doc}`../topics/staging`.

## Probe

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

The stand-in's `--version` text is `eb-standin`, so the framework version is unknown. A real EasyBuild version that the probe can parse is printed as `EasyBuild framework:`.

## Dry run

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
eb-staged-rebuild: software: /tmp/ebsr-docs-prefix/software
eb-staged-rebuild: modules: /tmp/ebsr-docs-prefix/modules
eb-staged-rebuild: state root: /tmp/ebsr-docs-prefix/.eb-staged-rebuild
eb-staged-rebuild: bwrap usable: False (bwrap is not installed)
eb-staged-rebuild: user.max_user_namespaces: 192727
eb-staged-rebuild: renameat2 RENAME_EXCHANGE on ext4: yes
eb-staged-rebuild: easybuild: eb-standin
eb-staged-rebuild: lock: local flock lock plus POSIX record lock; cross-node coherence unmeasured (open-004)
eb-staged-rebuild: publish: renameat2(RENAME_EXCHANGE), with a restoring ordinary-rename fallback
eb-staged-rebuild: command: /agent/repos/eb-staged-rebuild/docs/examples/eb-standin --installpath-software=/tmp/ebsr-docs-prefix/.eb-staged-rebuild/generations/<run-id>/software --installpath-modules=/tmp/ebsr-docs-prefix/.eb-staged-rebuild/generations/<run-id>/modules demo-1.eb --robot --rebuild
```

Dry-run writes no journal and does not call EasyBuild. The staged command is the install-path form, with `--rebuild` added because the arguments did not already contain `--rebuild`, `--force`, or `-f`.

## Publish, then roll back

The operator sequence, including a failed build, `resume`, `retire`, `rollback`, and `gc`, is {doc}`../howto/rebuild` and {doc}`../howto/recover`. Running the script end to end prints the same transcript:

```bash
WALKTHROUGH_ROOT=/tmp/ebsr-docs-prefix docs/examples/walkthrough.sh
```

On the recorded run, `resume` of the failed id `demo-run` printed `swapped all/demo/1.lua (renameat2-exchange)` and the live `PAYLOAD` became `payload-v2`. `rollback demo-run` restored `payload-v1` and left the replacement at `software/demo/.1.demo-run`.

`resolve` on the published module printed:

```text
{
  "complete": true,
  "kind": "file",
  "path": "/tmp/ebsr-docs-prefix/modules/all/demo/1.lua",
  "root": "/tmp/ebsr-docs-prefix/software/demo/1",
  "root_kind": "dir",
  "syntax": "lua"
}
```

The generation directory and the exchange are in {doc}`../topics/staging`. Prepare, the ledger flag, and the publication order are in {doc}`../howto/rebuild`. Every flag is in {doc}`../reference/cli`.

# eb-staged-rebuild

Rebuild an EasyBuild install without emptying the live prefix.

`eb --rebuild` and `eb --force` clear the install directory during the build. This tool installs into a new directory and then publishes that directory by replacing the module file. `software/<name>/<version>` stays a real directory. It is not renamed, and it is not replaced with a symlink.

The module file is the pointer. It is written to a temporary name in the same directory and moved into place with one `rename`. Jobs that already loaded the module keep the old paths. Jobs that load it afterwards see one complete generation. Old generations stay until a grace period has passed.

EasyBuild's `--installpath-software` and `--installpath-modules` are enough to build the new directory. `--bwrap` (EasyBuild 5.3 and later) is optional. The switch runs in this process, after `eb` exits, because hooks run inside the build and inside the sandbox when `--bwrap` is on.

Author: Rohit Goswami &lt;rgoswami@ieee.org&gt;. License: MIT.

## Install

Python 3.11 or newer. The standard library is enough.

```bash
python3 -m pip install .
# or, from this directory:
python3 -m eb_staged_rebuild --help
```

## Run

```bash
eb-staged-rebuild probe --path /path/to/software
eb-staged-rebuild run \
  --software "$EASYBUILD_INSTALLPATH_SOFTWARE" \
  --modules "$EASYBUILD_INSTALLPATH_MODULES" \
  --dry-run \
  -- file.eb --robot
eb-staged-rebuild run \
  --software "$EASYBUILD_INSTALLPATH_SOFTWARE" \
  --modules "$EASYBUILD_INSTALLPATH_MODULES" \
  -- file.eb --robot
```

`--software` and `--modules` are the live trees. They also come from `EASYBUILD_INSTALLPATH_SOFTWARE` and `EASYBUILD_INSTALLPATH_MODULES`. The state directory defaults to `<parent of software>/.eb-staged-rebuild`.

After a crash:

```bash
eb-staged-rebuild status --state-root /path/to/.eb-staged-rebuild
eb-staged-rebuild resume --state-root /path/to/.eb-staged-rebuild RUN_ID
eb-staged-rebuild rollback --state-root /path/to/.eb-staged-rebuild RUN_ID
```

`rollback` restores the previous module files and leaves the generation on disk. `gc` removes a retired generation only after `--grace-hours` (default 144, from the notes, not a measured site limit) and only under the state directory's `generations/` folder.

```bash
eb-staged-rebuild gc --state-root /path/to/.eb-staged-rebuild --dry-run
eb-staged-rebuild resolve /path/to/modules/all/Name/version.lua
```

The operator notes are in [docs/runbook.md](docs/runbook.md). The choice of design is in [DESIGN.md](DESIGN.md).

## Tests

```bash
python3 -m unittest discover -s tests
```

A real EasyBuild rebuild is skipped unless `EB_STAGED_REBUILD_INTEGRATION=1` and both `eb` and Lmod are on `PATH` (`LMOD_CMD` when Lmod is not already configured). With the variable set, a missing `eb` or Lmod is a failure.

## What this does not do

It does not make an in-place rebuild safe. It does not update `DT_RPATH` or scripts that embed the old directory. It does not claim `flock` or `RENAME_EXCHANGE` work on a filesystem this process has not probed. `open-004` (the site's bubblewrap, sysctl, rename flag, cross-node lock, and EasyBuild version) stays open.

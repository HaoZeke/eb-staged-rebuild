# eb-staged-rebuild

Rebuild an EasyBuild install in a staged tree, then exchange the complete tree
with the stable `software/<name>/<version>` directory.

The default backend tries EasyBuild bubblewrap, then a user/mount namespace
that makes both live trees read-only, then an unsandboxed install-path
fallback. Publish uses `renameat2(RENAME_EXCHANGE)` when the target filesystem
supports it. The ordinary-rename fallback restores the old live name after
every caught failure.

Author: Rohit Goswami &lt;rgoswami@ieee.org&gt;. License: MIT.

## Install

Python 3.11 or newer is required. The standard library is sufficient.

```bash
python3 -m pip install .
python3 -m eb_staged_rebuild --help
```

## Probe and run

```bash
eb-staged-rebuild probe --path "$EASYBUILD_INSTALLPATH_SOFTWARE"

eb-staged-rebuild run \
  --software "$EASYBUILD_INSTALLPATH_SOFTWARE" \
  --modules "$EASYBUILD_INSTALLPATH_MODULES" \
  --dry-run \
  -- example.eb --robot

eb-staged-rebuild run \
  --software "$EASYBUILD_INSTALLPATH_SOFTWARE" \
  --modules "$EASYBUILD_INSTALLPATH_MODULES" \
  -- example.eb --robot
```

`--backend auto` is the default. Use `--backend bwrap` or
`--backend userns` when an unsandboxed fallback must be forbidden. Use
`--backend installpath` only when the documented fallback is acceptable.

The state directory defaults to a hidden sibling of the software tree. It must
be on the same filesystem as the live installs.

## Recovery

```bash
eb-staged-rebuild status --state-root /path/to/.eb-staged-rebuild
eb-staged-rebuild resume --state-root /path/to/.eb-staged-rebuild RUN_ID
eb-staged-rebuild rollback --state-root /path/to/.eb-staged-rebuild RUN_ID
eb-staged-rebuild gc --state-root /path/to/.eb-staged-rebuild --dry-run
```

`rollback` exchanges the retired directory back into the stable path. For a
retire run it puts the module file back. `gc` deletes only a journaled hidden
sibling after the grace period; it never deletes the stable live path. The
default grace is 144 hours, the notes' unverified 120 hours plus a day.

When a package installed here is later published elsewhere on `MODULEPATH`
(for example a CVMFS repository), `retire` moves its module file out of the
module tree, so users load the other copy, without touching
`software/<name>/<version>`:

```bash
eb-staged-rebuild retire \
  --state-root /path/to/.eb-staged-rebuild \
  --module-file /path/to/modules/all/Name/version.lua \
  --reason "published on software.surf.nl" \
  --dry-run
```

Drop `--dry-run` to move the file. `rollback` with the printed
`retire-<timestamp>` id puts it back. `gc` lists the software directory as
ready to remove by hand once `--grace-hours` have passed. The state root must
be on the same filesystem as the module tree.

## Test

```bash
python3 -m unittest discover -s tests -v
```

Set `EB_STAGED_REBUILD_INTEGRATION=1` to enable the real EasyBuild/Lmod tests.
With that variable set, missing prerequisites are failures.

See [DESIGN.md](DESIGN.md), [the runbook](docs/runbook.md), and the Org-mode
[how-to](docs/orgmode/howto/staged-rebuild.org) and
[reference](docs/orgmode/reference/staged-rebuild.org).

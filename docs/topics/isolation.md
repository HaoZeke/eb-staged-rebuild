# Build isolation

`--backend auto` selects a backend in this order:

1. EasyBuild's bubblewrap backend, after `bwrap --dev-bind / / true` succeeds.
2. `unshare` with a user and mount namespace. The helper bind-mounts the live software tree and the live module tree read-only, then checks that a probe write fails with `EROFS`. The build's install path is the generation directory.
3. The install-path fallback. EasyBuild still receives a separate software tree and a separate module tree. There is no kernel sandbox. The command prints that the run is using this fallback.

On the walk-through host, bubblewrap was not installed and the user-namespace sandbox was usable, so `auto` would have selected `userns`. The recorded commands passed `--backend installpath` because `docs/examples/eb-standin` implements that backend.

An explicit `--backend bwrap` or `--backend userns` refuses when its probe fails. Sites that require a sandbox pass one of those and do not accept the fallback.

The fallback exists for hosts where bubblewrap is absent and unprivileged user namespaces are disabled. It still blocks EasyBuild's normal cleanup of the live tree, because the install path is the generation. A hook that writes outside its configured install path is outside that protection.

## Arguments removed from the staged command

`filter_eb_args` drops options that would send the build at the live tree or delete a previous install when the module file and the new directory differ:

* `--bwrap`, `--disable-bwrap`, `--remove-ghost-install-dirs`
* `--installpath`, `--installpath-software`, `--installpath-modules`, `--prefix`
* `--bwrap-installpath`, `--bwrap-options`, `--subdir-software`, `--subdir-modules`

`ensure_rebuild` appends `--rebuild` unless `--rebuild`, `--force`, or `-f` is already present. The dry-run transcript shows that appended flag.

The child environment sets `EASYBUILD_INSTALLPATH_SOFTWARE` and `EASYBUILD_INSTALLPATH_MODULES` to the generation (or, for bubblewrap, to the live trees, which the sandbox bind-mounts over) and puts the live module tree on `MODULEPATH`. `EASYBUILD_INSTALLPATH`, `EASYBUILD_PREFIX`, and `EASYBUILD_REMOVE_GHOST_INSTALL_DIRS` are removed.

## Bubblewrap stage keys

`probe` learns whether the selected EasyBuild writes stage paths into `bwrap_info.json` by importing `easybuild.tools.bwrap` with the Python beside `eb`. When `eb` is a wrapper with no Python beside it, a table read from the framework source answers for 5.3.0 and 5.3.1 (no stage keys; the 5.3 layout joins `<bwrap_installpath>/software` and `modules`), and for 5.4.0, 5.4.1, and 5.4.2 (keys present). Any other version reads as unknown. The JSON a build actually writes is what publication trusts. A missing stage path is not guessed, and an unidentified version without the keys is left unpublished.

The design note's `open-004` recommendation is to pin EasyBuild to 5.4.2 or a newer version the integration test has run. See {doc}`limits`.

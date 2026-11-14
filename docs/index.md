# eb-staged-rebuild

```{image} _static/logo-light.svg
:alt: eb-staged-rebuild
:width: 420px
:class: only-light
```

```{image} _static/logo-dark.svg
:alt: eb-staged-rebuild
:width: 420px
:class: only-dark
```

eb-staged-rebuild rebuilds an existing EasyBuild installation in a versioned generation directory, then exchanges that complete tree with the stable `software/<name>/<version>` directory and publishes the module file with a same-directory replace. The previous tree stays on disk as `software/<name>/.<version>.<run-id>`.

## Why

EasyBuild `--rebuild` and `--force` empty an existing install before the new install has passed its sanity checks. This program sends EasyBuild at `<state-root>/generations/<run-id>` and exchanges the live directory only after that generation is complete. `rollback` exchanges the hidden sibling back onto the stable path. `gc` deletes that sibling after the grace period, and leaves the stable path in place.

```{toctree}
:hidden:
:maxdepth: 2

intro/index
howto/index
topics/index
reference/index
development/index
```

::::{grid} 1 2 2 3
:gutter: 2

:::{grid-item-card} Get started
:link: intro/tutorial
:link-type: doc

Install the package, probe the filesystem, publish one generation, and roll it back.
:::

:::{grid-item-card} Installation
:link: intro/install
:link-type: doc

Python 3.11 or newer on Linux, with the packages used by the sandbox probes.
:::

:::{grid-item-card} Rebuild and publish
:link: howto/rebuild
:link-type: doc

Operator steps for a dry run, a build, and module publication.
:::

:::{grid-item-card} Recovery and rollback
:link: howto/recover
:link-type: doc

Resume a failed run, roll a published generation back, and collect hidden siblings.
:::

:::{grid-item-card} Staging model
:link: topics/staging
:link-type: doc

Generations, the hidden sibling, and the directory exchange.
:::

:::{grid-item-card} Reference
:link: reference/cli
:link-type: doc

Commands, environment variables, the Python API, and the changelog.
:::

::::

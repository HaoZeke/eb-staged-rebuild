# eb-staged-rebuild

eb-staged-rebuild rebuilds an existing EasyBuild installation in a versioned generation directory, then exchanges that complete tree with the stable `software/<name>/<version>` directory and publishes the module file with a same-directory replace. The previous tree stays on disk as `software/<name>/.<version>.<run-id>`.

EasyBuild `--rebuild` and `--force` empty an existing install before the new install has passed its sanity checks. This program sends EasyBuild at `<state-root>/generations/<run-id>` and exchanges the live directory only after that generation is complete.

Python 3.11 or newer on Linux. The package has no third-party dependencies.

```bash
python3 -m pip install .
eb-staged-rebuild --version
```

The documentation site (tutorial, operator how-to, staging model, recovery, API) is built from `docs/`:

```bash
python3 -m pip install -e ".[docs]"
make -C docs html
```

Open `docs/_build/html/index.html`. The source pages start at [docs/index.md](docs/index.md).

Author: Rohit Goswami &lt;rgoswami@ieee.org&gt;. License: the MIT license, see [LICENSE](LICENSE).

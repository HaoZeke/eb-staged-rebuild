# Testing

From the repository root, with the package importable:

```bash
python3 -m unittest discover -s tests -v
```

`tests/test_cli_docs.py` reads `README.md` and every Markdown file under `docs/` (other than `docs/_build`) and checks that each long option, subcommand, backend name, and environment variable read in `eb_staged_rebuild/cli.py` appears there.

Set `EB_STAGED_REBUILD_INTEGRATION=1` to enable `tests/test_easybuild_integration.py`. With that variable set, missing `eb` or Lmod is a failure. The bubblewrap case skips when `bwrap --dev-bind / / true` fails. The workflow that installs EasyBuild 5.4.2, bubblewrap, and Lmod is `.github/workflows/test.yml`.

The ledger tests need `EB_STACK_BIN`, an eb-stack 0.7.0 binary, and `openssl` on `PATH`. `tests/test_ledger_eb_stack.py` skips without the binary. The build-events and ledger-maintenance suites fail without it. The workflow builds that binary with `.github/actions/pinned-ledger` from eb-stack commit `defb6ae742ef9a56c23aa12280745423c8d46b9a`.

Documentation HTML is built with warnings treated as errors:

```bash
python3 -m pip install -e ".[docs]"
sudo apt-get install graphviz
make -C docs html
```

`sphinx-build -W -b html docs docs/_build/html` is the same build. The CI job `docs` runs it and does not deploy the site.

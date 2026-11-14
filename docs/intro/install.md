# Installation

The runtime is Linux. Publication uses `renameat2`, a local `flock`, a POSIX record lock, and `/proc` to confirm that a build's process group has stopped. Python 3.11 or newer is required. The package has no third-party dependencies.

`eb-staged-rebuild --version` prints `eb_staged_rebuild.__version__`. The command and the distribution metadata both identify version 0.4.4. The changelog records the release behaviour.

## The static binary

Each release carries one static binary per architecture (`x86_64`, `aarch64`), built with Zig 0.17.1 for `linux-musl`, a `SHA256SUMS` file and build provenance attestations:

```bash
v=1.0.0 arch=x86_64
curl -LO "https://github.com/stackhands/eb-staged-rebuild/releases/download/v${v}/eb-staged-rebuild-${v}-${arch}-linux-musl"
curl -LO "https://github.com/stackhands/eb-staged-rebuild/releases/download/v${v}/SHA256SUMS"
sha256sum --check --ignore-missing SHA256SUMS
gh attestation verify "eb-staged-rebuild-${v}-${arch}-linux-musl" --repo stackhands/eb-staged-rebuild
install -m 0755 "eb-staged-rebuild-${v}-${arch}-linux-musl" ~/.local/bin/eb-staged-rebuild
```

The binary needs nothing else at run time; the probe tools in the table below are the same. {doc}`../reference/compat` lists where its output differs from the Python tool on purpose. From a checkout, `zig build -Drelease=true` builds it into `zig-out/bin/`.

## From a checkout


This command was run from the repository root:

```bash
python3 -m pip install .
eb-staged-rebuild --version
```

```text
eb-staged-rebuild 0.4.4
```

A development install that also pulls the documentation tools is:

```bash
python3 -m pip install -e ".[docs]"
```

Build the HTML site from `docs/` (Graphviz's `dot` must be on `PATH`):

```bash
make -C docs html
```

The pages are written to `docs/_build/html/`.

## Debian and Ubuntu

The test workflow installs the optional probe tools with:

```bash
sudo apt-get update && sudo apt-get install -y bubblewrap lmod openssl acl
```

`unshare`, used by the user-namespace backend, comes from `util-linux`. EasyBuild 5.4.2 in that workflow is installed with:

```bash
python -m pip install easybuild-framework==5.4.2 easybuild-easyblocks==5.4.2 easybuild-easyconfigs==5.4.2
```

The integration tests that call this EasyBuild are enabled with `EB_STAGED_REBUILD_INTEGRATION=1`. See {doc}`../development/testing`.

## Fedora, RHEL, and Rocky

The matching package names are `bubblewrap`, `Lmod`, `openssl`, and `acl`. On RHEL and Rocky, `Lmod` is commonly taken from EPEL. `unshare` is in `util-linux`. The pip install from a checkout is the same command as on Debian.

## What the probes need

| Tool | Used by |
| --- | --- |
| `bwrap` | `--backend bwrap`, and `--backend auto` when the probe succeeds |
| `unshare` | `--backend userns`, and `--backend auto` when bubblewrap is unavailable |
| `eb` | the build, selected with `--eb` or `EB_STAGED_REBUILD_EB` |
| `eb-stack` 0.7.0 | `--ledger-config`, `build_events`, and `ledger_maintenance` |
| `openssl` | the ledger test suite, which generates keys |

`probe` reports which of the sandbox and exchange calls succeed on the account and the filesystem you point it at. See {doc}`../howto/rebuild`.

# Releasing

A release is a tag `vX.Y.Z` on `main`. The `release` workflow builds the
static Zig binaries for x86_64 and aarch64 and tests each on its own
architecture: the Zig unit tests, the differential, the crash and fail
matrices and the signal tests.

It refuses a release binary that contains a test hook
(`EB_STAGED_REBUILD_ACL_MODEL`, `EB_STAGED_REBUILD_FAIL_AT`), a build that
accepts `-Dtest-ops` with `-Drelease`, and a tag that disagrees with the
binary's `--version` or the Python `__version__`. It then opens a draft
release with the binaries, `SHA256SUMS` and build provenance attestations.

## Checklist

1. `__version__` in `eb_staged_rebuild/__init__.py`, `version` in
   `pyproject.toml` and `.version` in `build.zig.zon` name the new version.
2. `CHANGELOG.md` has a section for it.
3. `TESTMAP.md` has no row marked "not yet", and `COMPAT.md` lists every
   deliberate difference between the Zig build and the Python tool.
4. CI on `main` is green on both architectures, including the real-EasyBuild
   integration job (Python and the binary) and the ledger tests against a
   real eb-stack.
5. Tag and push: `git tag -a vX.Y.Z -m vX.Y.Z && git push origin vX.Y.Z`.
6. Check the draft release: two binaries, `SHA256SUMS`, attestations
   (`gh attestation verify FILE --repo stackhands/eb-staged-rebuild`), and a
   local build of the tag with the same sha256.
7. Publish the draft. Assets cannot change after that, so nothing is added
   to a published release.

# Ledger maintenance

`eb_staged_rebuild.ledger_maintenance` reads and maintains an eb-stack ledger: the JSON Lines log that `eb-stack ledger append` writes, hashed as an RFC 9162 Merkle log with Ed25519-signed heads. It adds no log format and no cryptography. Every signature comes from `eb-stack ledger head` or `eb-stack ledger append`, and every proof from `eb-stack ledger verify`. The commands work on local files only. They do not publish to a remote, install a scheduler or touch git.

```bash
python -m eb_staged_rebuild.ledger_maintenance COMMAND --eb-stack /abs/eb-stack --log /abs/ledger/stack.jsonl --public-key /abs/keys/ledger.pub ...
```

Every path must be absolute and normalised, and must contain no symbolic link. The log, the head files and the signing key must each be a file with exactly one link, and no two roles may name the same file. The signing key must sit outside the log's directory, the head's directory and the anchor directory. Exit status 0 means done. Exit status 2 means refused, with nothing appended or published. Exit status 75 means eb-stack gave no usable receipt (it failed, timed out or printed something unexpected); rerun the same command.

## Observed-history backfill

```bash
python -m eb_staged_rebuild.ledger_maintenance backfill ... \
  --key /abs/keys/ledger.pem --head /abs/heads/head.json \
  --manifest /abs/backfill/site-audit-1.json --manifest-sha256 <sha256 of the manifest file>
```

The manifest records what an operator saw, when they saw it, and where the evidence is:

```json
{
  "schema": "eb-staged-rebuild/observed-backfill/1",
  "manifest_id": "site-audit-1",
  "observed_at": "2026-11-02T09:30:00Z",
  "observer": "audit/jdoe",
  "stack": "2026.06",
  "target": "x86_64/generic",
  "observations": [
    {"module": "legacy/1.0", "location": "/sw/legacy/1.0",
     "modulefile": {"path": "/modules/all/legacy/1.0.lua", "sha256": "<hex>"},
     "provenance": [{"ref": "/archive/easybuild/legacy-1.0.log", "kind": "easybuild-log", "sha256": "<hex>"}],
     "note": "optional free text"}
  ]
}
```

Each observation becomes one `finding` record. The record's `ts` is `observed_at`, the time of the observation, which must not be later than the current time. The record never names an install or a build date, has no `stage`, and does not claim that anything succeeded. Its `build` object holds the manifest id, the manifest's SHA-256, `observed_at` and the original provenance references unchanged. Unknown fields are refused, so a manifest cannot carry an `installed_at`, an `action` or a `stage`.

A rerun with the same manifest sends byte-identical records, and eb-stack acknowledges them as already recorded. The manifest is immutable. Before anything is appended, the run is refused when the bytes do not hash to `--manifest-sha256`, when the same `manifest_id` was already backfilled from other bytes, or when one of its event ids is recorded with other content. An interrupted run is finished by rerunning it. A torn last line from an interrupted append is quarantined by eb-stack on the next append.

## Daily anchors

```bash
python -m eb_staged_rebuild.ledger_maintenance anchor ... --key /abs/keys/ledger.pem --anchors /abs/anchors
python -m eb_staged_rebuild.ledger_maintenance verify-anchors ... --anchors /abs/anchors
```

`anchor` signs the log's current head at the current UTC time with `eb-stack ledger head --ts` and publishes it as `<anchors>/<YYYY-MM-DD>.head.json` for that day. It never makes an anchor for any other day. Before publishing, `eb-stack ledger verify` must prove two things against a private copy of the log: the new head is the root of the log's first `size` records, and it extends the latest existing anchor. Otherwise the run is refused and no file is added. That covers a rewritten or truncated log, an anchor signed by another key, and two heads that disagree about the same history.

The anchor is written in a staging directory, made read-only, and hard-linked into place, so it appears whole or not at all. An existing anchor is never replaced. When today's anchor already exists, `anchor` proves that it still extends into the current log and reports `exists`. An anchor that has a second link, that is malformed, or whose signing date differs from its file name is refused and left as it is for inspection. An anchor dated after today is also refused, which catches a clock that has gone backwards. Runs are serialised by a lock in the anchor directory, and staging left by a crashed run is removed under that lock.

`verify-anchors` proves that each anchor extends the one before it and that the latest anchor binds to the log.

The anchor directory must not be the log's directory or contain it. The key must not be inside either directory. Copying anchors off the machine, and running `anchor` each day, are left to the site.

## Package history

```bash
python -m eb_staged_rebuild.ledger_maintenance project ... --head /abs/heads/head.json --out /abs/history
```

`project` reads the log and the head once and copies them into a private directory, where `eb-stack ledger verify` must accept them. The projection is then parsed from those same bytes. A head that covers fewer records than the log holds is stale and refused, so every projected record is covered by a signature. A tampered log or head, a head signed by another key, a log with a partial last line, and a verify receipt that is not exactly what eb-stack prints are all refused before anything is written.

It writes `index.json`, `index.md`, and `packages/<name>.json` plus `packages/<name>.md` for each software name, which is the module up to its first `/`. The output is a function of the verified log and head alone: keys are sorted, records are in log order, and no generation time is included. The same input always gives the same bytes. Package files are replaced one at a time, and the index is written last. An interrupted run leaves the previous index in place, and a rerun finishes the job. The output directory must be empty, or hold only a previous projection.

The history keeps every record. Nothing is dropped or rewritten:

* **Corrections.** A stageless or `finished` correction voids the record it `corrects`, which is then shown as `corrected`, listing the corrections that voided it. A `started` correction is listed as pending, and an `aborted` correction has no effect. A correction can itself be corrected.
* **Module state.** The state is derived only from records that are stageless or `finished` and that no correction has voided. `install`, `rebuild-inplace`, `rebuild-bump` and `unretire` mean installed, `retire` means retired, `remove` means removed, and `move` means moved.
* **Access and findings.** `access` records are listed per module and do not change the state. `finding` records are listed per module, and an observed backfill finding shows its manifest and provenance.
* **Problems.** A link to a record that is not in the log, and a correction that does not name an earlier record, are reported as problems.

Text from records is untrusted. JSON is written as ASCII. In Markdown, table and inline markup characters are backslash-escaped, `<`, `>` and `&` become entities, and line breaks, control characters and bidirectional overrides are shown as `U+XXXX`. Package file names are percent-encoded, so a name cannot leave `packages/`.

## Tests

`tests/test_ledger_maintenance.py` drives the real eb-stack binary named by `EB_STACK_BIN`, and uses openssl to make keys:

```bash
EB_STACK_BIN=/abs/eb-stack python -m unittest tests.test_ledger_maintenance -v
```

Without that binary, the real-ledger tests fail rather than skip. The input-validation tests need neither the binary nor openssl.

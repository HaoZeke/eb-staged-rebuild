# Environment and configuration

## Process environment

| Variable | Where it is read | Default |
| --- | --- | --- |
| `EASYBUILD_INSTALLPATH_SOFTWARE` | `--software` when the flag is omitted | required for `run` |
| `EASYBUILD_INSTALLPATH_MODULES` | `--modules` when the flag is omitted | required for `run` |
| `EB_STAGED_REBUILD_STATE_ROOT` | `--state-root` when the flag is omitted | `<parent of software>/.eb-staged-rebuild` |
| `EB_STAGED_REBUILD_EB` | `--eb` | `eb` |
| `EB_STAGED_REBUILD_GRACE_HOURS` | `--grace-hours` when the flag is omitted | 144 |
| `EB_STAGED_REBUILD_INTEGRATION` | `tests/test_easybuild_integration.py` | unset; the integration tests are skipped |
| `EB_STACK_BIN` | ledger tests | unset; `tests/test_ledger_eb_stack.py` skips, and the build-events and ledger-maintenance suites fail |

`EB_STANDIN_FAIL=1` is read only by `docs/examples/eb-standin`.

## Ledger configuration for `run`

`--ledger-config` is a JSON object. `load_config` in `eb_staged_rebuild.ledger_adapter` requires:

| Key | Content |
| --- | --- |
| `eb_stack` | Absolute path of the eb-stack executable |
| `log` | Absolute path of the JSON Lines log |
| `key` | Absolute path of the Ed25519 PKCS#8 signing key |
| `head` | Absolute path of the signed head file |
| `actor`, `stack`, `target` | Non-empty strings copied into each record |
| `action` | `install`, `rebuild-inplace` (default), or `rebuild-bump` |
| `public_key` | Optional absolute path. With it, a receipt also needs `eb-stack ledger verify` |
| `required` | Boolean, default `true` |
| `timeout` | Positive number of seconds, default 120 |

The record `location` is the stable install directory. The journal schema for that run is 4. The build-events configuration is a different object; it has `outbox` and no `head`. See {doc}`../howto/build-events`.

## State-root layout

| Path | Role |
| --- | --- |
| `<state-root>/.lock` | Local flock gate. Holds the holder's pid while the lock is taken. |
| `<state-root>/.lock.posix` | POSIX record-lock sidecar. |
| `<state-root>/generations/<run-id>/` | The generation EasyBuild writes. |
| `<state-root>/runs/<run-id>/journal.json` | The journal. |
| `<state-root>/runs/<run-id>/prepared/` | Module files about to be published. |
| `<state-root>/runs/<run-id>/backups/` | Previous module bytes. |
| `<state-root>/runs/<run-id>/prepare-context.json` | Argument to `--prepare`. |
| `<state-root>/runs/<run-id>/outbox.json` | Ledger events for this publication. |
| `<state-root>/retired/<timestamp>/` | Module files moved by `retire`. |
| `software/<name>/.<version>.<run-id>` | Hidden sibling beside the live install. Lives on the software filesystem, outside the state root. |

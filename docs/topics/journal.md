# Journal

Each run is `<state-root>/runs/<run-id>/journal.json`, written by a same-directory replace so readers see a complete file. The generation directory is `<state-root>/generations/<run-id>`.

## Schema

| Schema | Meaning |
| --- | --- |
| 2 | Directory swap. Stable path, staged path, and retired sibling are recorded. Retire journals are stored as schema 2 with `kind` set to `retire`. |
| 4 | Directory swap whose ledger policy is frozen. `publication` is `directory-swap` and `ledger_policy` is an object. |
| 1 | Generation-pointer journal. Refused. |
| 3 | Generation-pointer journal with that line's ledger policy. Refused. |

`load_journal` raises `NeedsIntervention` for schema 1 and schema 3. This program publishes by directory swap and does not apply a generation-pointer plan.

Run ids from the clock use UTC `YYYYMMDD-HHMMSS` plus the process id. `--run-id` replaces that. A second run with the same id is refused.

## States

| State | What it means |
| --- | --- |
| `created` | The journal exists and the build has not been recorded as finished. |
| `building` | EasyBuild has been started for this attempt. |
| `built` | The generation is planned and has not been published. |
| `publishing` | Directory exchange is in progress. |
| `complete` | Every planned module was published. |
| `failed` | The build exited non-zero, the live tree changed during the build, or planning refused the generation. Live modules were not changed. |
| `rolled_back` | Rollback finished, or a retire rollback finished. |

`resume` rebuilds from `created`, `building`, and `failed` when no module is `published`. It publishes from `built` and `publishing`. A crash during planning can leave prepared module files while the journal still says `building`; `resume` from `building` rebuilds that generation.

Retire journals use `retiring`, then `retired`, then `rolled_back`.

## What a directory-swap plan records

Each entry of `modules_plan` includes `rel`, `live_module`, `prepared`, `staged`, `live_software`, `retired`, `backup`, `new_hash`, `old_hash`, `status`, `swap_method`, and `published_at`. `links_plan` records class links. `manifests` is a content record of each live install taken before the build, unless `--skip-live-check` was set. Publish refuses when that record no longer matches, so a live tree that changed during the build is not exchanged.

The `demo-run` journal after rollback has `"schema": 2`, `"publication": "directory-swap"`, `"state": "rolled_back"`, `"backend": "installpath"`, and `"module_rels": ["demo/1"]`. The plan's `rel` is `all/demo/1.lua`, `live_software` ends in `software/demo/1`, and `retired` ends in `software/demo/.1.demo-run`.

`status` prints one line per journal and one line per module. A retire journal prints the software directory it left in place. There is no partial listing: the first journal that cannot be read stops the command.

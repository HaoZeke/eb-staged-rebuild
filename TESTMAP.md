# Tests that import the Python code, and what covers them for the Zig build

Port plan D4: every test that imports Python code maps to a replacement that
runs against the Zig build. A row marked "not yet" has no replacement; the
port of that command is not accepted while one is left.

## tests/test_ledger_maintenance.py (`ledger-maintenance`)

`tests/differential_ledger.py` builds one fixture per case, runs the Python tool and
the binary on two copies of it, and compares exit code, stdout, stderr and the files
left behind; a crash case stops the program at a named fault point and kills it.
Every test of the module has a replacement.

| test | replacement (a `tests/differential_ledger.py` case unless named otherwise) |
|---|---|
| InputTests.test_markdown_escaping | `zig/ledger.zig` test "md escapes markup, HTML and controls" |
| InputTests.test_file_stems_stay_in_one_directory | `zig/ledger.zig` test "file stems stay in one directory" |
| InputTests.test_heads_must_be_exactly_what_eb_stack_writes | `zig/ledger.zig` test "heads must be exactly what eb-stack writes" |
| InputTests.test_log_lines_must_be_canonical_ledger_records | `zig/ledger.zig` test "log lines must be canonical ledger records" |
| CommandLineTests.test_backfill_anchor_and_project_from_the_command_line | runs against `TOOL_BIN` |
| BackfillTests.test_observations_become_findings_at_the_observation_time | backfill: findings at the observation time, then a projection |
| BackfillTests.test_repeated_retries_append_nothing | backfill: findings at the observation time, then a projection (second backfill) |
| BackfillTests.test_an_interrupted_backfill_resumes_without_duplicates | backfill: an interrupted backfill resumes without duplicates |
| BackfillTests.test_a_torn_tail_is_quarantined_by_eb_stack_before_the_backfill | backfill: a torn tail is quarantined first |
| BackfillTests.test_changed_manifest_content_is_refused_before_anything_is_appended | backfill: changed manifest content is refused |
| BackfillTests.test_an_event_recorded_with_other_content_is_refused_before_anything_is_appended | backfill: an event recorded with other content is refused |
| BackfillTests.test_unpinned_or_guessed_input_is_refused | backfill: refused input, * (15 cases); backfill: an unpinned digest is refused |
| BackfillTests.test_path_aliases_and_a_key_beside_the_log_are_refused | backfill: path aliases and a key beside the log are refused |
| AnchorTests.test_the_first_anchor_is_read_only_single_link_and_verifies | anchor: the first anchor is read-only, single-link and verifies |
| AnchorTests.test_an_empty_log_can_be_anchored | anchor: an empty log |
| AnchorTests.test_a_same_day_retry_never_replaces_the_anchor | anchor: the first anchor is read-only, single-link and verifies (second anchor) |
| AnchorTests.test_daily_anchors_form_a_verified_chain | anchor: daily anchors form a verified chain |
| AnchorTests.test_a_rewritten_log_is_refused_and_no_anchor_is_added | anchor: a rewritten log is refused |
| AnchorTests.test_a_truncated_log_is_refused | anchor: a truncated log is refused |
| AnchorTests.test_a_tampered_or_malformed_anchor_is_refused_and_kept | anchor: a tampered anchor (signature, root, truncated, extra field) is refused and kept |
| AnchorTests.test_an_anchor_from_a_later_day_or_another_key_is_refused | anchor: an anchor from a later day is refused; anchor: a head signed by another key is refused |
| AnchorTests.test_interrupted_publication_leaves_no_partial_anchor | anchor: a crash at anchor.sign / anchor.publish leaves no anchor |
| AnchorTests.test_a_crash_between_link_and_cleanup_is_recovered | anchor: a crash after the link is recovered |
| AnchorTests.test_an_anchor_with_another_name_is_refused | anchor: an anchor whose name is not its day is refused; anchor: an anchor with a second link is refused |
| AnchorTests.test_an_index_checkpoint_line_after_the_head_receipt_is_accepted | anchor: receipt, index checkpoint |
| AnchorTests.test_malformed_head_receipts_are_refused | anchor: receipt, noisy head / lying verify / failing verify / failing head |
| AnchorTests.test_anchor_paths_are_checked | anchor: paths are checked |
| ProjectionTests.test_history_keeps_correction_retire_access_and_finding_semantics | project: history, corrections, findings and hostile text |
| ProjectionTests.test_projection_is_deterministic | project: history, corrections, findings and hostile text (three projections, two directories) |
| ProjectionTests.test_untrusted_text_is_escaped | project: history, corrections, findings and hostile text |
| ProjectionTests.test_a_tampered_log_is_refused_and_nothing_is_written | project: a edited / rewritten / partial log is refused and nothing is written |
| ProjectionTests.test_a_tampered_or_foreign_head_is_refused | project: tampered and foreign heads are refused |
| ProjectionTests.test_a_stale_head_is_refused | project: a stale head is refused |
| ProjectionTests.test_an_interrupted_projection_is_completed_by_a_rerun | project: an interrupted projection is completed by a rerun; project: a crash before the index is replaced keeps the old index |
| ProjectionTests.test_output_directory_is_checked | project: the output directory is checked |
| ProjectionTests.test_a_malformed_verify_receipt_is_refused | project: verify receipt, silent / doubled / tiles fail |

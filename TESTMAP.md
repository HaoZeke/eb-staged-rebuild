# Tests that import the Python code, and what covers them for the Zig build

Port plan D4: every test that imports Python code maps to a replacement that
runs against the Zig build. A row marked "not yet" has no replacement; the
port of that command is not accepted while one is left.

## tests/test_ledger_maintenance.py (`ledger-maintenance`)

`tests/differential_ledger.py` runs the Python tool and the binary on the same
fixture and compares exit code, stdout, stderr and the projection files.

| test | replacement |
|---|---|
| InputTests.test_markdown_escaping | `zig/ledger.zig` test "md escapes markup, HTML and controls" |
| InputTests.test_file_stems_stay_in_one_directory | `zig/ledger.zig` test "file stems stay in one directory" |
| InputTests.test_heads_must_be_exactly_what_eb_stack_writes | `zig/ledger.zig` test "heads must be exactly what eb-stack writes" |
| InputTests.test_log_lines_must_be_canonical_ledger_records | `zig/ledger.zig` test "log lines must be canonical ledger records" |
| CommandLineTests.test_backfill_anchor_and_project_from_the_command_line | runs against `TOOL_BIN` |
| BackfillTests.test_observations_become_findings_at_the_observation_time | differential: backfill, project after backfill |
| BackfillTests.test_repeated_retries_append_nothing | differential: backfill again appends nothing |
| BackfillTests.test_changed_manifest_content_is_refused_before_anything_is_appended | differential: backfill, wrong digest (the pinned-digest half) |
| BackfillTests.test_an_interrupted_backfill_resumes_without_duplicates | not yet |
| BackfillTests.test_a_torn_tail_is_quarantined_by_eb_stack_before_the_backfill | not yet |
| BackfillTests.test_an_event_recorded_with_other_content_is_refused_before_anything_is_appended | not yet |
| BackfillTests.test_unpinned_or_guessed_input_is_refused | not yet |
| BackfillTests.test_path_aliases_and_a_key_beside_the_log_are_refused | not yet |
| AnchorTests.test_the_first_anchor_is_read_only_single_link_and_verifies | differential: anchor, verify-anchors |
| AnchorTests.test_a_same_day_retry_never_replaces_the_anchor | differential: anchor again |
| AnchorTests.test_daily_anchors_form_a_verified_chain | not yet (needs two days) |
| AnchorTests.test_an_empty_log_can_be_anchored | not yet |
| AnchorTests.test_a_rewritten_log_is_refused_and_no_anchor_is_added | not yet |
| AnchorTests.test_a_truncated_log_is_refused | not yet |
| AnchorTests.test_a_tampered_or_malformed_anchor_is_refused_and_kept | not yet |
| AnchorTests.test_an_anchor_from_a_later_day_or_another_key_is_refused | not yet |
| AnchorTests.test_interrupted_publication_leaves_no_partial_anchor | not yet |
| AnchorTests.test_a_crash_between_link_and_cleanup_is_recovered | not yet |
| AnchorTests.test_an_anchor_with_another_name_is_refused | not yet |
| AnchorTests.test_an_index_checkpoint_line_after_the_head_receipt_is_accepted | `zig/ledger.zig` test "summary and receipt lines" (the line parser only) |
| AnchorTests.test_malformed_head_receipts_are_refused | not yet |
| AnchorTests.test_anchor_paths_are_checked | not yet |
| ProjectionTests.test_history_keeps_correction_retire_access_and_finding_semantics | differential: project (fixture has corrections, retire, access, findings) |
| ProjectionTests.test_projection_is_deterministic | differential: project again |
| ProjectionTests.test_untrusted_text_is_escaped | differential: project (bidi, `|`, `<`, `*` in names and reasons) |
| ProjectionTests.test_a_stale_head_is_refused | differential: stale head |
| ProjectionTests.test_output_directory_is_checked | differential: project into the ledger directory (one case) |
| ProjectionTests.test_a_tampered_log_is_refused_and_nothing_is_written | not yet |
| ProjectionTests.test_a_tampered_or_foreign_head_is_refused | not yet |
| ProjectionTests.test_an_interrupted_projection_is_completed_by_a_rerun | not yet |
| ProjectionTests.test_a_malformed_verify_receipt_is_refused | not yet |

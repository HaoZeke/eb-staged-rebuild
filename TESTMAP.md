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

## Access metadata of the atomic write (zig-common `atomic.atomicWrite`)

The Zig build writes module files with zig-common's `atomicWrite`. These tests are ported one for one to zig-common `src/atomic_test.zig`,
which emulates each ACL model at the descriptor calls (`atomic.ops`), spies on the kernel's POSIX ACLs and
forks a child that exits at a fault point for a crash. Each row names the Zig test.

| test | replacement |
|---|---|
| test_acl_models.AclModelTests.test_native_bytes_round_trip_through_descriptors_after_chmod | zig-common `src/atomic_test.zig` test "native bytes round trip through descriptors after chmod" |
| test_acl_models.AclModelTests.test_projection_is_never_written_in_place_of_the_native_acl | zig-common `src/atomic_test.zig` test "a projection is never written in place of the native ACL" |
| test_acl_models.AclModelTests.test_projection_that_disagrees_with_the_native_acl_is_refused | zig-common `src/atomic_test.zig` test "a projection that disagrees with the native ACL is refused" |
| test_acl_models.AclModelTests.test_denied_list_is_refused_not_treated_as_absence | zig-common `src/atomic_test.zig` test "a denied list is refused, not treated as absence" |
| test_acl_models.AclModelTests.test_unsupported_list_is_refused | zig-common `src/atomic_test.zig` test "an unsupported list is refused" |
| test_acl_models.AclModelTests.test_denied_read_of_a_listed_acl_is_refused | zig-common `src/atomic_test.zig` test "a denied read of a listed ACL is refused" |
| test_acl_models.AclModelTests.test_listed_then_unreadable_acl_is_refused | zig-common `src/atomic_test.zig` test "a listed, then unreadable ACL is refused" |
| test_acl_models.AclModelTests.test_denied_probe_of_an_unlisted_acl_name_is_refused | zig-common `src/atomic_test.zig` test "a denied probe of an unlisted ACL name is refused" |
| test_acl_models.AclModelTests.test_readable_virtual_acl_is_preserved_when_listing_is_empty | zig-common `src/atomic_test.zig` test "a readable virtual ACL is preserved when the listing is empty" |
| test_acl_models.AclModelTests.test_unknown_system_attribute_is_refused | zig-common `src/atomic_test.zig` test "an unknown system attribute is refused" |
| test_acl_models.AclModelTests.test_attributes_changed_during_inspection_are_refused | zig-common `src/atomic_test.zig` test "attributes changed during inspection are refused" |
| test_acl_models.AclModelTests.test_ownership_change_during_inspection_is_refused | zig-common `src/atomic_test.zig` test "an ownership or mode change during inspection is refused" |
| test_acl_models.AclModelTests.test_source_acl_change_before_rename_is_refused | zig-common `src/atomic_test.zig` test "a source ACL change before the rename is refused" |
| test_acl_models.AclModelTests.test_denied_acl_write_is_refused_and_leaves_no_temporary_file | zig-common `src/atomic_test.zig` test "a denied ACL write is refused and leaves no temporary file" |
| test_acl_models.AclModelTests.test_written_acl_that_reads_back_differently_is_refused | zig-common `src/atomic_test.zig` test "a written ACL that reads back differently is refused" |
| test_acl_models.AclModelTests.test_mode_recomputed_by_acl_restoration_is_refused | zig-common `src/atomic_test.zig` test "a mode recomputed by ACL restoration is refused" |
| test_acl_models.AclModelTests.test_inherited_acl_is_removed_when_the_source_has_none | zig-common `src/atomic_test.zig` test "an inherited ACL is removed when the source has none" |
| test_acl_models.AclModelTests.test_denied_removal_of_an_inherited_acl_is_refused | zig-common `src/atomic_test.zig` test "a denied removal of an inherited ACL is refused" |
| test_acl_models.AclModelTests.test_symlink_is_still_refused_before_inspection | zig-common `src/atomic_test.zig` test "a symlink is still refused before inspection" |
| test_acl_models.AclModelTests.test_symlinked_metadata_source_is_refused | zig-common `src/atomic_test.zig` test "a symlinked metadata source is refused" |
| test_acl_probe_compatibility.AclProbeCompatibilityTests.test_unlisted_einval_acl_probe_is_treated_as_not_provided | zig-common `src/atomic_test.zig` test "an unlisted EINVAL probe is treated as not provided when POSIX is listed" |
| test_acl_probe_compatibility.AclProbeCompatibilityTests.test_listed_einval_acl_read_is_refused_not_treated_as_absent | zig-common `src/atomic_test.zig` test "a listed EINVAL read is refused, not treated as absent" |
| test_acl_probe_compatibility.AclProbeCompatibilityTests.test_unlisted_einval_does_not_weaken_eacces_or_eperm_probes | zig-common `src/atomic_test.zig` test "an unlisted EINVAL does not weaken EACCES or EPERM probes" |
| test_acl_probe_compatibility.AclProbeCompatibilityTests.test_unlisted_nfs4_family_einval_with_listed_posix_retains_posix_bytes | zig-common `src/atomic_test.zig` test "an unlisted NFSv4-family EINVAL with a listed POSIX ACL keeps the POSIX bytes" |
| test_acl_probe_compatibility.AclProbeCompatibilityTests.test_unlisted_posix_einval_without_positive_model_is_refused | zig-common `src/atomic_test.zig` test "an unlisted POSIX EINVAL without a positive model is refused" |
| test_acl_probe_compatibility.AclProbeCompatibilityTests.test_unlisted_nfs4_einval_without_listed_posix_is_refused | zig-common `src/atomic_test.zig` test "an unlisted NFSv4 EINVAL without a listed POSIX ACL is refused" |
| test_acl_probe_compatibility.AclProbeCompatibilityTests.test_listed_nfs4_einval_is_refused | zig-common `src/atomic_test.zig` test "a listed NFSv4 EINVAL is refused" |
| test_acl_probe_compatibility.AclProbeCompatibilityTests.test_listed_gpfs_einval_is_refused | zig-common `src/atomic_test.zig` test "a listed GPFS EINVAL is refused" |
| test_acl_probe_compatibility.AclProbeCompatibilityTests.test_narrow_einval_guard_still_refuses_denied_nfs4_probe | zig-common `src/atomic_test.zig` test "the narrow EINVAL guard still refuses a denied NFSv4 probe" |
| test_acl_probe_compatibility.AclProbeCompatibilityTests.test_narrow_einval_guard_still_refuses_changing_metadata | zig-common `src/atomic_test.zig` test "the narrow EINVAL guard still refuses changing metadata" |
| test_posix_acl_descriptor.PosixAclDescriptorTests.test_mask_wider_than_group_owner_round_trips_with_its_mode | zig-common `src/atomic_test.zig` test "a mask wider than the group owner round-trips with its mode" |
| test_posix_acl_descriptor.PosixAclDescriptorTests.test_mask_narrower_than_group_owner_round_trips_with_its_mode | zig-common `src/atomic_test.zig` test "a mask narrower than the group owner round-trips with its mode" |
| test_posix_acl_descriptor.PosixAclDescriptorTests.test_requested_mode_that_contradicts_the_mask_is_refused | zig-common `src/atomic_test.zig` test "a requested mode that contradicts the mask is refused" |
| test_posix_acl_descriptor.PosixAclDescriptorTests.test_acl_is_written_through_the_descriptor_after_ownership_and_mode | zig-common `src/atomic_test.zig` test "the ACL is written through the descriptor after ownership and mode" |
| test_posix_acl_descriptor.PosixAclDescriptorTests.test_acl_removed_between_list_and_read_is_refused | zig-common `src/atomic_test.zig` test "an ACL removed between list and read is refused" |
| test_posix_acl_descriptor.PosixAclDescriptorTests.test_acl_replaced_between_reads_is_refused | zig-common `src/atomic_test.zig` test "an ACL replaced between reads is refused" |
| test_posix_acl_descriptor.PosixAclDescriptorTests.test_acl_changed_before_rename_is_refused | zig-common `src/atomic_test.zig` test "an ACL changed before the rename is refused" |
| test_posix_acl_descriptor.PosixAclDescriptorTests.test_denied_descriptor_operations_are_refused | zig-common `src/atomic_test.zig` test "denied descriptor operations are refused" |
| test_posix_acl_descriptor.PosixAclDescriptorTests.test_inherited_default_acl_removal_failure_is_refused | zig-common `src/atomic_test.zig` test "an inherited default ACL that cannot be removed is refused" |
| test_atomic_metadata.AtomicMetadataTests.test_replacement_preserves_owner_group_mode_and_access_acl | zig-common `src/atomic_test.zig` test "a replacement keeps owner, group, mode and the access ACL" |
| test_atomic_metadata.AtomicMetadataTests.test_replacement_does_not_gain_the_parent_default_acl | zig-common `src/atomic_test.zig` test "a replacement does not gain the parent's default ACL" |
| test_atomic_metadata.AtomicMetadataTests.test_acl_copy_failure_preserves_the_live_bytes | zig-common `src/atomic_test.zig` test "an ACL copy failure keeps the live bytes" |
| test_atomic_metadata.AtomicMetadataTests.test_ownership_copy_failure_preserves_the_live_bytes | zig-common `src/atomic_test.zig` test "an ownership copy failure keeps the live bytes" |
| test_atomic_metadata.AtomicMetadataTests.test_resume_replaces_an_owned_readonly_temporary_file | zig-common `src/atomic_test.zig` test "a resume replaces an owned read-only temporary file" |
| test_atomic_metadata.AtomicMetadataTests.test_hardlinked_temporary_file_cannot_modify_another_file | zig-common `src/atomic_test.zig` test "a hard-linked temporary file cannot modify another file" |
| test_virtual_acl_restore.VirtualAclRestoreTests.test_required_posix_acl_replaces_an_unremovable_virtual_view | zig-common `src/atomic_test.zig` test "a required POSIX ACL replaces an unremovable virtual view" |
| test_virtual_acl_restore.VirtualAclRestoreTests.test_virtual_view_that_survives_required_acl_restoration_is_refused | zig-common `src/atomic_test.zig` test "a virtual view that survives the required ACL is refused" |
| test_virtual_acl_restore.VirtualAclRestoreTests.test_absent_source_acl_does_not_accept_an_unremovable_virtual_view | zig-common `src/atomic_test.zig` test "an absent source ACL does not accept an unremovable virtual view" |
| test_stale_temporary.StaleTemporaryTests.test_metadata_refusal_removes_its_temporary_file | zig-common `src/atomic_test.zig` test "a metadata refusal removes its temporary file" |
| test_stale_temporary.StaleTemporaryTests.test_fsync_error_removes_its_temporary_file | zig-common `src/atomic_test.zig` test "an fsync error removes its temporary file" |
| test_stale_temporary.StaleTemporaryTests.test_a_temporary_file_replaced_by_another_writer_is_not_removed | zig-common `src/atomic_test.zig` test "a temporary file replaced by another writer is not removed" |
| test_stale_temporary.StaleTemporaryTests.test_privileged_resume_accepts_a_temporary_owned_by_the_module_owner | zig-common `src/atomic_test.zig` test "a privileged resume accepts a temporary owned by the module owner" |
| test_stale_temporary.StaleTemporaryTests.test_privileged_write_refuses_a_foreign_temporary_without_a_metadata_owner | zig-common `src/atomic_test.zig` test "a privileged write refuses a foreign temporary without a metadata owner" |
| test_stale_temporary.StaleTemporaryTests.test_privileged_write_still_refuses_a_hardlinked_temporary | zig-common `src/atomic_test.zig` test "a privileged write still refuses a hard-linked temporary" |
| test_stale_temporary.StaleTemporaryTests.test_privileged_write_refuses_a_temporary_directory | zig-common `src/atomic_test.zig` test "a privileged write refuses a temporary directory" |

## Publication metadata through `run` and `rollback`

`tests/differential_metadata.py` runs both tools on one fixture with the kernel's POSIX ACLs and compares exit
code, stdout, stderr and the module file's bytes, mode and ACL.

| test | replacement |
|---|---|
| test_publication_metadata.PublicationMetadataTests.test_new_module_preserves_staged_acl_with_restrictive_umask | differential_metadata: a new module keeps the staged ACL under a restrictive umask |
| test_publication_metadata.PublicationMetadataTests.test_finalizer_metadata_is_applied_at_publication | differential_metadata: the prepare policy's ACL is applied at publication |
| test_publication_metadata.PublicationMetadataTests.test_rollback_restores_previous_mode_and_absence_of_acl | differential_metadata: rollback restores the previous mode and the absence of an ACL |
| test_acl_publication.AclPublicationTests.test_publication_and_rollback_keep_native_acl_bytes | POSIX: differential_metadata (publication, rollback); GPFS and NFSv4 models: zig-common `src/atomic_test.zig` "native bytes round trip through descriptors after chmod" |
| test_acl_publication.AclPublicationTests.test_missing_module_file_takes_the_staged_native_acl | POSIX: differential_metadata (new module keeps the staged ACL); other models: zig-common `src/atomic_test.zig` |
| test_acl_publication.AclPublicationTests.test_uninspectable_prepared_acl_refuses_before_any_publication | differential_metadata: a prepared module replaced by a symlink is refused before publication |
| test_acl_publication.AclPublicationTests.test_denied_acl_write_mid_publication_is_refused_and_rolls_back | not yet: the write refusal is zig-common `src/atomic_test.zig` "a denied ACL write is refused"; the roll-back of the module already published is not run against the binary |
| test_acl_publication.AclPublicationTests.test_live_acl_change_during_backup_refuses_that_publication | not yet: the refusal is zig-common `src/atomic_test.zig` "a source ACL change before the rename is refused"; the engine step is not run against the binary |
| test_acl_publication.AclPublicationTests.test_unsupported_destination_model_refuses_publication | not yet: the refusal is zig-common `src/atomic_test.zig` "an unknown system attribute is refused"; the engine step is not run against the binary |

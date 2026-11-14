# Tests that import the Python code, and what covers them for the Zig build

Port plan D4: every test that imports Python code maps to a replacement that
runs against the Zig build. A row marked "not yet" has no replacement; the
port of that command is not accepted while one is left.
`tests/check_testmap.py` (CI, unit job) fails on a missing row, a row that
names no such test, or a "not yet" row.

## tests/test_ledger_maintenance.py (`ledger-maintenance`)

`tests/differential_ledger.py` builds one fixture per case, runs the Python tool and
the binary on two copies of it, and compares exit code, stdout, stderr and the files
left behind; a crash case stops the program at a named fault point and kills it.
Every test of the module has a replacement.

| test | replacement (a `tests/differential_ledger.py` case unless named otherwise) |
|---|---|
| test_ledger_maintenance.InputTests.test_markdown_escaping | `zig/ledger.zig` test "md escapes markup, HTML and controls" |
| test_ledger_maintenance.InputTests.test_file_stems_stay_in_one_directory | `zig/ledger.zig` test "file stems stay in one directory" |
| test_ledger_maintenance.InputTests.test_heads_must_be_exactly_what_eb_stack_writes | `zig/ledger.zig` test "heads must be exactly what eb-stack writes" |
| test_ledger_maintenance.InputTests.test_log_lines_must_be_canonical_ledger_records | `zig/ledger.zig` test "log lines must be canonical ledger records" |
| test_ledger_maintenance.CommandLineTests.test_backfill_anchor_and_project_from_the_command_line | runs against `TOOL_BIN` |
| test_ledger_maintenance.BackfillTests.test_observations_become_findings_at_the_observation_time | backfill: findings at the observation time, then a projection |
| test_ledger_maintenance.BackfillTests.test_repeated_retries_append_nothing | backfill: findings at the observation time, then a projection (second backfill) |
| test_ledger_maintenance.BackfillTests.test_an_interrupted_backfill_resumes_without_duplicates | backfill: an interrupted backfill resumes without duplicates |
| test_ledger_maintenance.BackfillTests.test_a_torn_tail_is_quarantined_by_eb_stack_before_the_backfill | backfill: a torn tail is quarantined first |
| test_ledger_maintenance.BackfillTests.test_changed_manifest_content_is_refused_before_anything_is_appended | backfill: changed manifest content is refused |
| test_ledger_maintenance.BackfillTests.test_an_event_recorded_with_other_content_is_refused_before_anything_is_appended | backfill: an event recorded with other content is refused |
| test_ledger_maintenance.BackfillTests.test_unpinned_or_guessed_input_is_refused | backfill: refused input, * (15 cases); backfill: an unpinned digest is refused |
| test_ledger_maintenance.BackfillTests.test_path_aliases_and_a_key_beside_the_log_are_refused | backfill: path aliases and a key beside the log are refused |
| test_ledger_maintenance.AnchorTests.test_the_first_anchor_is_read_only_single_link_and_verifies | anchor: the first anchor is read-only, single-link and verifies |
| test_ledger_maintenance.AnchorTests.test_an_empty_log_can_be_anchored | anchor: an empty log |
| test_ledger_maintenance.AnchorTests.test_a_same_day_retry_never_replaces_the_anchor | anchor: the first anchor is read-only, single-link and verifies (second anchor) |
| test_ledger_maintenance.AnchorTests.test_daily_anchors_form_a_verified_chain | anchor: daily anchors form a verified chain |
| test_ledger_maintenance.AnchorTests.test_a_rewritten_log_is_refused_and_no_anchor_is_added | anchor: a rewritten log is refused |
| test_ledger_maintenance.AnchorTests.test_a_truncated_log_is_refused | anchor: a truncated log is refused |
| test_ledger_maintenance.AnchorTests.test_a_tampered_or_malformed_anchor_is_refused_and_kept | anchor: a tampered anchor (signature, root, truncated, extra field) is refused and kept |
| test_ledger_maintenance.AnchorTests.test_an_anchor_from_a_later_day_or_another_key_is_refused | anchor: an anchor from a later day is refused; anchor: a head signed by another key is refused |
| test_ledger_maintenance.AnchorTests.test_interrupted_publication_leaves_no_partial_anchor | anchor: a crash at anchor.sign / anchor.publish leaves no anchor |
| test_ledger_maintenance.AnchorTests.test_a_crash_between_link_and_cleanup_is_recovered | anchor: a crash after the link is recovered |
| test_ledger_maintenance.AnchorTests.test_an_anchor_with_another_name_is_refused | anchor: an anchor whose name is not its day is refused; anchor: an anchor with a second link is refused |
| test_ledger_maintenance.AnchorTests.test_an_index_checkpoint_line_after_the_head_receipt_is_accepted | anchor: receipt, index checkpoint |
| test_ledger_maintenance.AnchorTests.test_malformed_head_receipts_are_refused | anchor: receipt, noisy head / lying verify / failing verify / failing head |
| test_ledger_maintenance.AnchorTests.test_anchor_paths_are_checked | anchor: paths are checked |
| test_ledger_maintenance.ProjectionTests.test_history_keeps_correction_retire_access_and_finding_semantics | project: history, corrections, findings and hostile text |
| test_ledger_maintenance.ProjectionTests.test_projection_is_deterministic | project: history, corrections, findings and hostile text (three projections, two directories) |
| test_ledger_maintenance.ProjectionTests.test_untrusted_text_is_escaped | project: history, corrections, findings and hostile text |
| test_ledger_maintenance.ProjectionTests.test_a_tampered_log_is_refused_and_nothing_is_written | project: a edited / rewritten / partial log is refused and nothing is written |
| test_ledger_maintenance.ProjectionTests.test_a_tampered_or_foreign_head_is_refused | project: tampered and foreign heads are refused |
| test_ledger_maintenance.ProjectionTests.test_a_stale_head_is_refused | project: a stale head is refused |
| test_ledger_maintenance.ProjectionTests.test_an_interrupted_projection_is_completed_by_a_rerun | project: an interrupted projection is completed by a rerun; project: a crash before the index is replaced keeps the old index |
| test_ledger_maintenance.ProjectionTests.test_output_directory_is_checked | project: the output directory is checked |
| test_ledger_maintenance.ProjectionTests.test_a_malformed_verify_receipt_is_refused | project: verify receipt, silent / doubled / tiles fail |

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
| test_acl_publication.AclPublicationTests.test_publication_and_rollback_keep_native_acl_bytes | POSIX: differential_metadata (publication, rollback); GPFS and NFSv4 models: zig-common `src/atomic_test.zig` "native bytes round trip through descriptors after chmod"; every model: `tests/test_acl_publication_cli.py` |
| test_acl_publication.AclPublicationTests.test_missing_module_file_takes_the_staged_native_acl | POSIX: differential_metadata (new module keeps the staged ACL); other models: zig-common `src/atomic_test.zig`; every model: `tests/test_acl_publication_cli.py` |
| test_acl_publication.AclPublicationTests.test_uninspectable_prepared_acl_refuses_before_any_publication | differential_metadata: a prepared module replaced by a symlink is refused before publication; every model: `tests/test_acl_publication_cli.py` |
| test_acl_publication.AclPublicationTests.test_denied_acl_write_mid_publication_is_refused_and_rolls_back | `tests/test_acl_publication_cli.py` (the `-Dtest-ops` test artifact, each ACL model) |
| test_acl_publication.AclPublicationTests.test_live_acl_change_during_backup_refuses_that_publication | `tests/test_acl_publication_cli.py` (the `-Dtest-ops` test artifact, each ACL model) |
| test_acl_publication.AclPublicationTests.test_unsupported_destination_model_refuses_publication | `tests/test_acl_publication_cli.py` (the `-Dtest-ops` test artifact, each ACL model) |

## tests/test_cluster_lock.py (zig-common `state.RootLock`)

The Zig build takes the state-root lock with zig-common's `RootLock`; these tests are ported to zig-common `src/state_test.zig`,
with failing calls injected through `state.lock_ops` and the other process a forked child.

| test | replacement |
|---|---|
| test_cluster_lock.ClusterLockTests.test_record_lock_excludes_an_independent_kernel_gate | zig-common `src/state_test.zig` test "the record lock excludes an independent kernel gate" |
| test_cluster_lock.ClusterLockTests.test_failed_local_contenders_preserve_record_lock | zig-common `src/state_test.zig` test "failed local contenders keep the record lock" |
| test_cluster_lock.ClusterLockTests.test_double_acquire_preserves_both_locks | zig-common `src/state_test.zig` test "a second acquire by the holder keeps both locks" (Zig: a second acquire is Busy, not Python's RuntimeError; both locks are asserted held after it) |
| test_cluster_lock.ClusterLockTests.test_acquisition_errors_release_descriptors_and_gate | zig-common `src/state_test.zig` test "acquisition errors release the descriptors and the gate" |
| test_cluster_lock.ClusterLockTests.test_sidecar_open_failure_releases_local_gate | zig-common `src/state_test.zig` test "a sidecar open failure releases the local gate" |
| test_cluster_lock.ClusterLockTests.test_fork_child_release_does_not_unlock_parent | zig-common `src/state_test.zig` test "a forked child's release does not unlock the parent" |
| test_cluster_lock.ClusterLockTests.test_release_error_still_closes_both_layers | zig-common `src/state_test.zig` test "a release error still closes both layers" |
| test_cluster_lock.ClusterLockTests.test_sidecar_hardlink_is_refused | zig-common `src/state_test.zig` test "a hard-linked sidecar is refused" |

## tests/test_probe_cli.py: the lock wait

| test | replacement |
|---|---|
| test_probe_cli.LockTests.test_second_lock_does_not_wait_forever | zig-common `src/state.zig` test "RootLock refuses a second holder and is free after release"; the timed wait: `src/state_test.zig` test "a timed acquire does not wait forever" |

## Commands that change a fixture: run, resume, rollback, retire, gc, probe, resolve

Five test programs run the Python tool and the binary on separate copies of one fixture and compare what each leaves:

- `tests/differential.sh`: each `compare` or `compare_admin "LABEL"` line is one case, and a row cites the
  LABEL. It compares exit code, stdout and stderr, and for `compare_admin` also the journals and the file
  tree, after normalizing paths, times and hashes.
- `tests/crash_matrix.py`: a run, a rollback, a retire and a retire's rollback are paused at every fault step
  the Python tool reaches, killed with SIGKILL, then resumed or rerun. Fixtures: one module, two modules
  (`lib/1`, `app/1`), a `--prepare` policy that counts its runs, a sealed installation (read-only trees) and
  a forced ordinary-rename fallback. A row cites "FIXTURE: run killed at STEP" and its rollback forms.
- `tests/fail_matrix.py`: against the `-Dtest-ops` artifact, `EB_STAGED_REBUILD_FAIL_AT=STEP` makes a step
  fail with an I/O error in both implementations (the Python fault injector raises `OSError(EIO)`), for
  every step of a run and a rollback, with one and two modules.
- `tests/signal_harness.py`: SIGTERM or SIGINT to a run while its build command runs, a command that exits
  with stray workers, and a retire while another process has the state lock.
- `tests/test_easybuild_integration_cli.py`: the real-EasyBuild tests with `TOOL_BIN run` in place of the
  Python engine (`EB_STAGED_REBUILD_INTEGRATION=1`, EasyBuild 5.4.2 and Lmod).

Python-only internals are covered by the Zig unit test of the same function, named as
`zig/FILE.zig` test "NAME".

### tests/test_engine.py

| test | replacement |
|---|---|
| TransactionTests.test_success_uses_fallback_and_keeps_old_tree_for_rollback | crash_matrix "fallback: run published" (journal `swap_method` `rename-fallback`, old tree kept as the hidden sibling) and "fallback: rollback killed at rollback.swap:all/demo/1.lua.fallback.*" |
| TransactionTests.test_success_uses_rename_exchange_when_probe_succeeds | differential "run, installpath" (journal `swap_method` `renameat2-exchange`) |
| TransactionTests.test_every_recorded_failure_restores_old_tree_then_resumes | fail_matrix "one module: run fails at STEP", every step |
| TransactionTests.test_failure_on_second_module_restores_both_modules | fail_matrix "two modules: run fails at STEP", every step of `lib/1` and `app/1` |
| TransactionTests.test_failed_build_never_touches_live_tree | differential "run, eb fails" |
| TransactionTests.test_dry_run_creates_no_state_and_changes_nothing | differential "run --dry-run, installpath", "run --dry-run, auto", "run --dry-run, userns" |
| TransactionTests.test_missing_rebuild_target_is_refused_before_build | differential "run, no installed target" |
| TransactionTests.test_symlink_rebuild_target_is_refused_before_build | differential "run, symlinked target" |
| TransactionTests.test_module_path_cannot_escape_software_root | differential "run, module escapes" |
| TransactionTests.test_symlinked_state_root_is_refused | differential "run, symlinked state root" |
| RollbackTests.test_rollback_restores_exact_old_directory | differential "rollback the run", "rollback the run again" |
| RollbackTests.test_every_rollback_failure_restores_published_tree | fail_matrix "one module: rollback fails at STEP", every step |
| GarbageCollectionTests.test_gc_removes_only_the_hidden_retired_tree_after_grace | differential "gc keeps within grace", "gc removes past grace", "gc again" |

### tests/test_retire.py

| test | replacement |
|---|---|
| RetireTests.test_retire_moves_module_and_leaves_software | differential "retire" |
| RetireTests.test_dry_run_changes_nothing | differential "retire --dry-run" |
| RetireTests.test_dry_run_still_refuses | differential "retire --dry-run, a directory" |
| RetireTests.test_resume_refuses_a_retire_record | differential "resume a retire record" |
| RetireTests.test_rollback_puts_module_back | differential "rollback the retire", "rollback again" |
| RetireTests.test_rollback_refuses_to_overwrite_a_new_module_file | differential "rollback over a new module file", "rollback after the new file is gone" |
| RetireTests.test_retire_again_after_rollback | differential "retire after a rollback" |
| RetireTests.test_refuses_already_retired | differential "retire again" |
| RetireTests.test_refuses_non_regular_files | differential "retire, a directory", "retire, a symlinked module file", "retire, no module file" |
| RetireTests.test_refuses_module_without_root | differential "retire, no root assignment" |
| RetireTests.test_refuses_when_lock_is_held | signal_harness "retire while the state lock is held: refused, module kept" |
| RetireTests.test_refuses_different_filesystem | differential "retire, state root on another filesystem" (where `/dev/shm` is writable) |
| RetireTests.test_crash_before_rename_leaves_module_and_rolls_back | crash_matrix "retire killed at retire.fsync", "retire killed at retire.rename" (a second retire is refused, the rollback restores the module file) |
| RetireTests.test_crash_after_rename_rolls_back | crash_matrix "retire killed at journal.retired.write", ".fsync", ".rename" |
| RetireGcTests.test_software_dir_outside_generations_is_reported_not_deleted | differential "gc keeps within grace", "gc removes past grace" (both after "retire") |
| RetireGcTests.test_rolled_back_retire_keeps_directory_referenced | differential "gc after the retire is rolled back" |
| RetireGcTests.test_directory_still_resolved_by_another_module_is_kept | differential "retire an alias", "rollback the alias retire", "retire the module again", "gc keeps a directory a live module names" |
| RetireGcTests.test_swapped_publish_then_retire_keeps_live_tree_and_drops_hidden_sibling | differential "retire a published module", "gc after a published module is retired" |

### tests/test_swap.py

The Python tests pass their own `rename` and `exchange_call`; the Zig tests replace the same calls through
`swap.ops`, which only a test build or the `-Dtest-ops` artifact can change.

| test | replacement |
|---|---|
| SwapTests.test_exchange_swaps_the_complete_directories | `zig/swap.zig` test "swap exchanges two sibling directories and back" |
| SwapTests.test_exchange_failure_after_success_restores_live | `zig/swap.zig` test "an exchange whose sync fails is exchanged back"; fail_matrix "one module: run fails at swap:all/demo/1.lua.exchange.after" |
| SwapTests.test_non_fallback_exchange_error_leaves_live_unchanged | `zig/swap.zig` test "an exchange error other than unsupported leaves live unchanged" |
| SwapTests.test_unsupported_exchange_uses_fallback | `zig/swap.zig` test "an unsupported exchange uses the fallback" |
| SwapTests.test_every_injected_fallback_failure_restores_live | `zig/swap.zig` test "every failed fallback rename or sync restores live" (the injected step precedes the rename that fails) |
| SwapTests.test_every_rename_error_in_fallback_restores_live | `zig/swap.zig` test "every failed fallback rename or sync restores live" |
| SwapTests.test_recover_after_first_fallback_rename | `zig/swap.zig` test "recovery after the first fallback rename" |
| SwapTests.test_recover_after_second_fallback_rename | `zig/swap.zig` test "recovery after the second fallback rename" |

### tests/test_modulefile.py

| test | replacement |
|---|---|
| ModuleFileTests.test_lua_and_tcl_loads_are_dependencies | `zig/modulefile.zig` test "loads, identity and rewriting" |
| ModuleFileTests.test_parse_lua_and_tcl | `zig/modulefile.zig` test "parseRoot follows the Python patterns"; differential "resolve lua.lua", "resolve tcl" |
| ModuleFileTests.test_rewrite_does_not_eat_a_longer_prefix | `zig/modulefile.zig` test "loads, identity and rewriting" |
| ModuleFileTests.test_new_path_that_contains_the_old_path_is_not_doubled | `zig/modulefile.zig` test "a new path that contains the old path is not doubled" |
| ModuleFileTests.test_refuses_a_file_with_no_root | `zig/modulefile.zig` test "loads, identity and rewriting" |

### tests/test_gc_references.py

| test | replacement |
|---|---|
| ReferenceTests.test_unreadable_module_is_not_treated_as_unreferenced | differential "gc, an unreadable live module" (not as root) |
| ReferenceTests.test_symlink_to_a_hidden_tree_keeps_it | differential "gc keeps within grace" (`kept/.3.0001`, named through the symlink `kept/alias`, is kept: a live module resolves inside it) |

### tests/test_backends.py

A fake `bwrap` and `unshare` first on PATH answer the probes (`FAKE_BWRAP_FAIL`, `FAKE_UNSHARE_FAIL` make them
refuse), and the fake `eb` answers `--version` with `FAKE_EB_VERSION`.

| test | replacement |
|---|---|
| FakeEbTests.test_installpath_swaps_the_staged_generation | differential "run, installpath" |
| FakeEbTests.test_bwrap_swaps_the_staged_generation | differential "run, bwrap" |
| FakeEbTests.test_userns_build_mounts_live_trees_read_only_then_swaps | differential "run, userns" |
| FakeEbTests.test_easybuild_53_layout_without_stage_keys | differential "run, bwrap, 5.3 layout without stage keys" |
| FakeEbTests.test_missing_stage_key_on_unknown_version_is_not_published | differential "run, bwrap, unknown release without stage keys" |
| FakeEbTests.test_overlay_upper_is_not_published | differential "run, bwrap, overlay" |
| FakeEbTests.test_bwrap_refuses_when_probe_fails | differential "run, bwrap probe fails" |
| FakeEbTests.test_auto_prefers_bwrap | differential "run, auto prefers bwrap" |
| FakeEbTests.test_auto_uses_installpath_when_no_sandbox_is_available | differential "run, auto with no sandbox uses installpath" |
| FakeEbTests.test_userns_command_mounts_both_live_trees_read_only | differential "run, userns", "run --dry-run, userns" |
| BwrapInfoTests.test_legacy_versions_use_the_documented_layout | differential "run, bwrap, 5.3 layout without stage keys" |
| BwrapInfoTests.test_present_keys_win | differential "run, bwrap" |
| BwrapInfoTests.test_overlay_is_refused | differential "run, bwrap, overlay" |

### tests/test_prepare_recovery.py

| test | replacement |
|---|---|
| PrepareRecoveryTests.test_empty_program_is_refused_by_the_api | differential "run, empty prepare" |
| PrepareRecoveryTests.test_empty_program_is_refused_before_cli_dispatch | differential "run, empty prepare" |
| PrepareRecoveryTests.test_failed_policy_retry_preserves_the_built_generation | differential "run, prepare fails", "resume after the prepare is fixed" (resumed from built, no build), "the prepare ran once" |
| PrepareRecoveryTests.test_child_signal_has_a_shell_exit_status | `zig/builders.zig` test "a command killed by a signal has a shell exit status" |
| PrepareRecoveryTests.test_policy_journal_requires_schema_two | differential "run, prepare fails" (journal `schema` compared) |

### tests/test_prepare_publication.py

| test | replacement |
|---|---|
| PreparePublicationTests.test_finalizer_restricts_generation_before_live_module_changes | differential "run, prepare restricts the staged tree", "the context the prepare saw", "modes after a restricting prepare" |
| PreparePublicationTests.test_finalizer_nonzero_exit_preserves_live_module | differential "run, prepare fails" |
| PreparePublicationTests.test_missing_finalizer_preserves_live_module | differential "run, missing prepare" |
| PreparePublicationTests.test_resume_from_built_runs_unfinished_finalizer | crash_matrix "prepare: run killed at STEP" for the steps from journal.built to prepare.invoke (the policy runs once) |
| PreparePublicationTests.test_resume_during_publication_does_not_repeat_finalizer | crash_matrix "prepare: run killed at STEP" for the steps after journal.prepared.rename (the policy runs once) |

### tests/test_prepare_barrier.py

| test | replacement |
|---|---|
| PrepareBarrierTests.test_incomplete_second_generation_prevents_first_publication | differential "run, prepare barrier, second generation incomplete", "module files after the barrier" |
| PrepareBarrierTests.test_unreadable_second_generation_prevents_first_publication | differential "run, prepare barrier, second generation unreadable", "module files after the barrier" |

### tests/test_easybuild_integration.py

| test | replacement |
|---|---|
| EasyBuildIntegrationTests.test_installpath_backend_keeps_the_live_file | `tests/test_easybuild_integration_cli.py` CliEasyBuildIntegrationTests.test_installpath_backend_keeps_the_live_file |
| EasyBuildIntegrationTests.test_bwrap_backend_keeps_the_live_file | `tests/test_easybuild_integration_cli.py` CliEasyBuildIntegrationTests.test_bwrap_backend_keeps_the_live_file |
| EasyBuildIntegrationTests.test_symlink_name_version_is_refused | `tests/test_easybuild_integration_cli.py` CliEasyBuildIntegrationTests.test_symlink_name_version_is_refused |

### tests/test_sealed_install.py

| test | replacement |
|---|---|
| SealedInstallTests.test_publish_preserves_sealed_files_and_permissions | crash_matrix "sealed: run killed at STEP" and "sealed: rollback killed at STEP" (published tree keeps 0555/0444, rollback restores the old tree) |
| SealedInstallTests.test_resume_publishes_sealed_generation_without_rebuilding | crash_matrix "sealed: run killed at journal.publishing.write" and every later step (exactly one build) |

### tests/test_hard_kill.py

| test | replacement |
|---|---|
| HardKillTests.test_sigkill_during_publish_fallback_resumes_once | crash_matrix "fallback: run killed at swap:all/demo/1.lua.fallback.*" |
| HardKillTests.test_sigkill_right_after_rename_exchange_resumes_to_published | crash_matrix "one module: run killed at swap:all/demo/1.lua.exchange.after" and "one module: run killed at swap:all/demo/1.lua.exchange.after, resumed, rolled back" |
| HardKillTests.test_sigkill_right_after_rollback_exchange_rolls_back_once | crash_matrix "one module: rollback killed at rollback.swap:all/demo/1.lua.exchange.after" |
| HardKillTests.test_sigkill_during_rollback_fallback_is_idempotent | crash_matrix "fallback: rollback killed at rollback.swap:all/demo/1.lua.fallback.*" |

### tests/test_command_lifecycle.py

| test | replacement |
|---|---|
| CommandLifecycleTests.test_sigterm_holds_publication_lock_until_command_group_stops | signal_harness "SIGTERM, command ignores SIGTERM: lock held until the group stops" |
| CommandLifecycleTests.test_interrupt_stops_descendants_when_the_direct_child_exits_first | signal_harness "SIGINT, direct child exits first: descendants stopped" |

### tests/test_command_group_exit.py

| test | replacement |
|---|---|
| CommandGroupExitTests.test_normal_exit_returns_after_stray_workers_have_exited | signal_harness "command exits with stray workers: run returns after they exited"; `zig/builders.zig` test "runCommand returns the exit code and stops the group" |
| CommandGroupExitTests.test_interrupted_command_returns_after_every_worker_has_exited | signal_harness "SIGTERM with stray workers: run returns after every worker exited" |

### tests/test_version.py

| test | replacement |
|---|---|
| Version.test_module_version_matches_pyproject | differential "--version" and its check "same version as build.zig.zon" |

### tests/test_readonly_cleanup.py

| test | replacement |
|---|---|
| ReadonlyCleanupTests.test_resume_can_replace_a_readonly_module_stage | crash_matrix "sealed: run killed at build.finish" (the module stage is read-only; resume clears it and publishes) |

### tests/test_cli_docs.py

| test | replacement |
|---|---|
| CliDocTests.test_docs_name_every_cli_option | differential "top level --help", "run -h", "resume -h", "rollback -h", "status -h", "gc -h", "retire -h", "probe -h", "resolve -h": the binary's help equals the Python parser's, which this test checks against the docs |

### tests/test_probe_cli.py

| test | replacement |
|---|---|
| ProbeTests.test_unreadable_resolve_exits_2_without_a_traceback | differential "resolve an unreadable file" (not as root) |
| ProbeTests.test_rename_exchange_reports_a_result_for_this_directory | differential "probe --json", "probe text" |
| ProbeTests.test_probe_all_has_the_open_questions | differential "probe --json" |
| CliTests.test_dry_run_and_probe_and_resolve | runs against `TOOL_BIN` |
| BwrapKeyTableTests.test_easybuild_5_4_1_writes_the_stage_path_keys | differential "probe, EasyBuild 5.4.1 stage keys" |
| BwrapKeyTableTests.test_easybuild_5_4_1_without_the_keys_is_refused_not_guessed | differential "run, bwrap, 5.4.1 without stage keys" |

## tests/test_ledger_bounded.py (the bounded `eb-stack ledger` call)

`publish.zig` `bounded()` calls zig-common `proc.run` with the options `_bounded` implies: a 64 KiB stdout and stderr limit
with the rest read and dropped (`stdout_overflow = .truncate`), `end_with_leader` and `wait_for_group`. The process-group
tests are ported to zig-common `src/proc_test.zig` with those options; the two receipt tests run through `build-events`
in `tests/test_ledger_bounded_cli.py`, against `TOOL_BIN` or the Python module.

| test | replacement |
|---|---|
| test_ledger_bounded.BoundedOutputTests.test_continuous_output_is_never_stored | zig-common `src/proc_test.zig` test "continuous output is never stored and the deadline still holds" |
| test_ledger_bounded.BoundedOutputTests.test_oversized_streams_keep_the_leading_limit_and_the_exit_code | zig-common `src/proc_test.zig` test "oversized streams keep the leading limit and the exit code" |
| test_ledger_bounded.BoundedOutputTests.test_writing_descendants_are_stopped_when_the_leader_exits | zig-common `src/proc_test.zig` test "writing descendants are stopped when the leader exits" |
| test_ledger_bounded.BoundedOutputTests.test_a_writer_that_left_the_group_cannot_hold_the_call_or_outlive_its_pipe | zig-common `src/proc_test.zig` test "a writer that left the group cannot hold the call or outlive its pipe" |
| test_ledger_bounded.BoundedOutputTests.test_timeout_returns_after_every_group_member_has_exited | zig-common `src/proc_test.zig` test "a timeout returns after every group member has exited" |
| test_ledger_bounded.BoundedOutputTests.test_interrupted_call_returns_after_every_group_member_has_exited | zig-common `src/proc_test.zig` test "an interrupted call returns after every group member has exited" (Zig: the `cancel` flag a signal handler sets, in place of Python's in-process signal) |
| test_ledger_bounded.BoundedOutputTests.test_a_noisy_helper_cannot_outlive_a_killed_caller | zig-common `src/proc_test.zig` test "a noisy helper cannot outlive a killed caller" |
| test_ledger_bounded.ExactReceiptTests.test_receipt_that_fills_the_output_limit_is_accepted | `tests/test_ledger_bounded_cli.py` test_receipt_that_fills_the_output_limit_is_accepted (the replay comes from a retry after `fail-after-append`) |
| test_ledger_bounded.ExactReceiptTests.test_receipt_beyond_the_output_limit_is_not_read | `tests/test_ledger_bounded_cli.py` test_receipt_beyond_the_output_limit_is_not_read |
## tests/test_ledger_directory_swap.py (`run`, `resume` and `rollback` with a ledger)

The differential's ledger cases run with a fake eb-stack (`tests/fake_eb_stack.py`) and compare the ledger
it writes, with ids, times and the state root's scope normalized.

| test | replacement |
|---|---|
| DirectorySwapLedgerTests.test_records_name_the_stable_directory_and_use_schema_4 | differential "run, ledger", "status, ledger run", "ledger records so far" (journal `schema` 4 and `publication`; records name `demo/1` at the stable directory with action `rebuild-inplace`) |
| DirectorySwapLedgerTests.test_required_ledger_failure_does_not_swap | differential "run, ledger append fails", "resume delivers the record" |
| DirectorySwapLedgerTests.test_policy_change_is_refused_and_the_stable_tree_stays | differential "resume after the ledger actor changed" |
| DirectorySwapLedgerTests.test_rollback_correction_is_idempotent | differential "rollback a ledger run", "rollback a ledger run again", "ledger records so far" (one correction pair naming the finished record) |
| DirectorySwapLedgerTests.test_generation_pointer_schema_is_not_applied | differential "resume a generation-pointer record", "status of a generation-pointer record" |

## tests/test_ledger_policy.py

| test | replacement |
|---|---|
| LedgerPolicyTests.test_required_policy_cannot_change_while_a_record_is_pending | differential "run, ledger append fails again", "resume after the ledger became optional" |
| LedgerPolicyTests.test_completed_run_cannot_redirect_its_events_to_another_ledger | differential "resume a completed run with its ledger redirected", "ledger records after the policy cases" |
| LedgerPolicyTests.test_policy_is_frozen_before_the_build_starts | differential "run, the build changes the ledger policy", "ledger records after the policy cases" |

## tests/test_ledger_eb_stack.py

| test | replacement |
|---|---|
| RealEbStackLedgerTests.test_append_recovery_and_idempotence | `tests/test_ledger_eb_stack_cli.py` CliEbStackLedgerTests.test_append_recovery_and_idempotence (a real eb-stack; the run is killed with SIGKILL at `ledger.acked:*`, resumed twice, rolled back twice; Python and the binary) |

## tests/test_build_events.py (`build-events`)

`Case.cli` and `Case.popen` run `TOOL_BIN build-events` when `TOOL_BIN` is set, so every test that drives the command runs
unchanged against the binary (CI: "build events and the bounded ledger call against the binary"; the real-ledger class
needs `EB_STACK_BIN`). The tests that call `build_events` in process run their same bodies through the command in
`tests/test_build_events_cli.py`: a crash pauses the command at the step the Python recorder lists
(`EB_STAGED_REBUILD_PAUSE_AT`) and kills it with SIGKILL; a refusal is exit 2 with the message on stderr.

| test | replacement |
|---|---|
| test_build_events.LifecycleTests.test_started_is_in_the_ledger_before_the_command_and_finished_after | runs against `TOOL_BIN` |
| test_build_events.LifecycleTests.test_command_failure_records_aborted_and_keeps_its_status | runs against `TOOL_BIN` |
| test_build_events.LifecycleTests.test_failure_with_a_lost_aborted_ack_keeps_the_command_status_and_names_the_ledger_error | runs against `TOOL_BIN` |
| test_build_events.LifecycleTests.test_success_with_a_lost_finished_ack_is_not_reported_as_success | runs against `TOOL_BIN` |
| test_build_events.LifecycleTests.test_missing_executable_is_aborted_not_started | runs against `TOOL_BIN` |
| test_build_events.LifecycleTests.test_outputs_that_do_not_check_turn_a_zero_exit_into_aborted | runs against `TOOL_BIN` |
| test_build_events.LifecycleTests.test_optional_mode_runs_the_command_and_keeps_events_pending | runs against `TOOL_BIN` |
| test_build_events.AcknowledgementTests.test_lost_started_ack_blocks_the_command_until_a_retry_is_acknowledged | runs against `TOOL_BIN` |
| test_build_events.AcknowledgementTests.test_crash_before_and_after_the_append_is_retried_with_the_same_bytes | `tests/test_build_events_cli.py` CrashCliTests.test_crash_before_and_after_the_append_is_retried_with_the_same_bytes (the same test body; pauses at the step and SIGKILLs the command) |
| test_build_events.AcknowledgementTests.test_exact_replay_appends_nothing_and_does_not_rerun | runs against `TOOL_BIN` |
| test_build_events.AcknowledgementTests.test_changed_metadata_is_refused_before_anything_is_appended | runs against `TOOL_BIN` |
| test_build_events.AcknowledgementTests.test_reused_operation_with_another_payload_is_refused_by_the_ledger | runs against `TOOL_BIN` |
| test_build_events.AcknowledgementTests.test_tampered_outbox_record_is_never_acknowledged | runs against `TOOL_BIN` |
| test_build_events.AcknowledgementTests.test_unreachable_ledger_blocks_the_command | runs against `TOOL_BIN` |
| test_build_events.AcknowledgementTests.test_crash_at_every_step_converges_without_duplicates_or_early_commands | `tests/test_build_events_cli.py` CrashCliTests.test_crash_at_every_step_converges_without_duplicates_or_early_commands (the same test body; pauses at the step and SIGKILLs the command) |
| test_build_events.ProcessTests.test_interrupted_command_is_aborted_after_its_whole_group_stopped | runs against `TOOL_BIN` |
| test_build_events.ProcessTests.test_interrupt_during_started_delivery_stops_the_ledger_group_and_never_runs_the_command | runs against `TOOL_BIN` |
| test_build_events.ProcessTests.test_producer_killed_during_the_command_is_recorded_as_unknown_and_not_rerun | runs against `TOOL_BIN` |
| test_build_events.ProcessTests.test_a_second_writer_of_the_same_operation_is_refused_while_the_first_holds_it | runs against `TOOL_BIN` |
| test_build_events.ProcessTests.test_environment_and_arguments_are_never_serialized | runs against `TOOL_BIN` |
| test_build_events.UnsafeInputTests.test_unsafe_configuration_is_refused_before_anything_is_written | `tests/test_build_events_cli.py` UnsafeInputCliTests.test_unsafe_configuration_is_refused_before_anything_is_written (the same test body; the command's exit 2 and stderr) |
| test_build_events.UnsafeInputTests.test_unsafe_metadata_is_refused | `tests/test_build_events_cli.py` UnsafeInputCliTests.test_unsafe_metadata_is_refused (the same test body; the command's exit 2 and stderr) |
| test_build_events.UnsafeInputTests.test_symlinked_operation_directory_is_refused | runs against `TOOL_BIN` |
| test_build_events.UnsafeInputTests.test_a_call_off_the_main_thread_is_refused_before_started_is_appended | no command-line form: it guards Python's `run_build` API, whose stop handlers need the main thread; the binary is single-threaded and installs them in `main` |
| test_build_events.UnsafeInputTests.test_oversized_metadata_is_refused | runs against `TOOL_BIN` |
| test_build_events.UnsafeInputTests.test_cli_needs_exactly_one_of_a_command_and_deliver | runs against `TOOL_BIN` |
| test_build_events.RealLedgerTests.test_clean_run_is_verified_by_inclusion_proofs | runs against `TOOL_BIN` |
| test_build_events.RealLedgerTests.test_lost_ack_before_the_append_blocks_and_retries | runs against `TOOL_BIN` |
| test_build_events.RealLedgerTests.test_lost_ack_after_the_append_is_replayed_once | runs against `TOOL_BIN` |
| test_build_events.RealLedgerTests.test_exact_replay_and_changed_payload_against_the_real_ledger | runs against `TOOL_BIN` |
| test_build_events.RealLedgerTests.test_independent_writers_share_one_ledger | runs against `TOOL_BIN` |

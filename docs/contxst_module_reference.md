# Module Reference (Source-Oriented)

## Purpose
This document maps each important module to its real responsibilities, major functions, and operational role.

## `importcsv.py` (client core + CLI)

### What it owns
- TurnPoint login and client page extraction.
- Document and budget download automation.
- Client duplicate guard and event recording.
- Package discovery and package bundle generation.
- Batch manifest processing.

### Notable function groups
- Logging and session wiring: `set_log_sink`, `log_message`.
- Duplicate handling: `DuplicateClientError`, `guard_against_duplicate`, `create_duplicate_report`.
- Output roots and naming: `configure_client_context`, `finalize_output_directory`, `assign_universal_sequence`.
- Scraping primitives: `extract_fields_on_page`, page-specific `extract_*` functions.
- Download helpers: `snapshot_downloads`, `wait_for_new_download`, `download_document_files`, `download_budget_excel`.
- Package dataset tools:
  - `find_purgeable_clients`
  - `bundle_package_download`
  - `collect_clients_by_package`
- Batch logic:
  - `load_client_manifest`
  - `build_batch_queue`
  - `run_client_batch`
- Appointment discovery CLI orchestration:
  - `--discover-item-numbers`
  - `--probe-client-id`
  - `--merge-service-types`
  - `--discovery-debug`
- CLI parsing: `parse_cli_args`, `main`.

### Behavioral notes
- Uses many global runtime variables for context (`CLIENT_ID`, `OUTPUT_DIR`, `FILE_PREFIX`, credentials).
- Continues extraction on per-page failures (logs errors, does not immediately abort the full run).
- Finalization and state updates occur after browser teardown.
- `--merge-service-types` is currently a compatibility path; `run_service_type_merge(...)` is a no-op stub.
- `--discovery-debug` is retained for compatibility and does not currently change extractor behavior.

## `appointment_item_discovery.py` (Service Type variants truth extraction)

### What it owns
- TP1 Add Appointment nested-iframe entry (`appointment-edit.asp` -> inner Assist `/appointments/new?...has_parent=true`) with strict capability gate.
- Service Type combobox selection using primary XPath + listbox/option fallback.
- Fixed six-pair variant extraction (`day/eve/night/saturday/sunday/ph`) via stable `data-cy` inputs.
- Variant row extraction (`Service`, `Rate`, `Code`, `Unit`) with raw + normalized fields plus TruthView aliases.
- Continue-on-error per Service Type (`Status=FAIL` rows + artifacts, then continue).
- Structured diagnostics (`events.jsonl`, `checkers.csv`) with HTML/PNG/console failure artifacts.
- Checkpointing and resumable extraction under LineItemRates checkpoints.

### Core functions
- Extraction pipeline:
  - `discover_appointment_item_numbers(...)`
  - `_open_assist_appointments_new(...)`
  - `_switch_to_variants_capable_editor(...)`
  - `_find_service_type_input(...)`
  - `_select_service_type_option(...)`
  - `_select_assist_option_with_retries(...)`
  - `_read_variant_fingerprint(...)`
  - `_wait_for_fingerprint_change(...)`
  - `_extract_variant_table_rows(...)`
  - `_build_variant_record(...)`
  - `_load_service_types_from_reference_index(...)`
  - `extract_service_type_variants(...)`
- Legacy compatibility:
  - `run_service_type_merge(...)`
  - `load_discovery_latest(...)`

### Output contract
- Variants:
  - `~/LineItemRates/ServiceTypeTruth/variants/latest/ServiceTypeVariants_latest.csv`
  - `~/LineItemRates/ServiceTypeTruth/variants/latest/ServiceTypeVariants_latest.xlsx`
  - `~/LineItemRates/ServiceTypeTruth/variants/snapshots/ServiceTypeVariants_<run_id>.csv/.xlsx`
  - Optional conflicts file: `ServiceTypeVariants_conflicts_<run_id>.csv`
- Per-row schema includes aliases used by TruthView import:
  - `Parent Service Type`
  - `Service Type ID`
  - `Item Number`
  - conflict fields: `Conflict`, `Conflict Detail`
- Checkpoints:
  - `~/LineItemRates/ServiceTypeTruth/variants/checkpoints/checkpoint_<run_id>.json`
  - `~/LineItemRates/ServiceTypeTruth/variants/checkpoints/variants_append_<run_id>.csv`
- Diagnostics per run under:
  - `~/LineItemRates/ServiceTypeTruth/variants/diagnostics/<run_id>/`
- Diagnostics persistence is in `finally` to avoid early-failure evidence loss.

## `turnpoint_purger_ui.py` (Tkinter app)

### What it owns
- Desktop UI for client and worker purge operations.
- Threaded orchestration of long-running tasks.
- Live log display, progress bars, cooldown controls, and manifests as table views.
- Nexis worker scan/preview/upload controls.

### Main areas in the UI
- Tab 1: Client Purger.
- Tab 2: Worker Purger.
- Tab 3: NexisUploader.
- Shared log panel at the bottom.

### Key interaction methods
- Client lifecycle: `_handle_engage`, `_execute_purge`, `_handle_purge_all_clients`.
- Worker lifecycle: `_handle_worker_engage`, `_execute_worker_purge`, `_handle_worker_purge_all`.
- Discovery workflows:
  - Clients: `_handle_collect_packages`, `_handle_find_purgeable_clients`, `_handle_bundle_download`.
  - Workers: `_handle_collect_workers`, `_handle_download_workers_excel`.
- Nexis workflows:
  - Scan: `_handle_nexis_scan`
  - Preview: `_handle_nexis_select`
  - Upload: `_handle_nexis_upload`
  - Combine/export: `_handle_combine_nexis`, `_handle_combine_clients`

### Threading model
- UI remains responsive by running backend operations in daemon threads.
- Logs pass through a queue and are drained by `after` callbacks.
- Button state is managed to avoid double-click races.

## `worker_purger.py` (worker core)

### What it owns
- Worker discovery and manifest generation from `carers.asp`.
- Worker profile extraction from `user-edit.asp`.
- Worker-specific duplicate guard, sequence assignment, and state recording.

### Core functions
- Discovery: `collect_workers`, `_extract_worker_rows`, `_write_worker_manifest`.
- Excel snapshot: `download_worker_excel`.
- Worker extraction: `_extract_form_fields`, `_build_worker_payload`.
- Output writers: `_write_csv`, `_write_qualification_csv`, `_write_allowance_csv`.
- Orchestration: `run_worker_purge`, `run_worker_batch`.

### Output contract
Per worker archive typically contains:
- `{SEQ} WorkerDetail.csv`
- `{SEQ} Qualification.csv`
- `{SEQ} Allowance.csv`
- `{SEQ} Documents/` directory (created even when not used)

## `nexis_uploader.py` (mapping layer)

### What it owns
- Reads worker detail CSVs.
- Converts internal worker fields into Nexis employee payload schema.
- Provides preview JSON for UI.

### Mapping logic highlights
- Date parser accepts multiple TurnPoint formats.
- Gender normalization: male/female fallback to OTHER.
- Department mapping from team labels.
- Fallback values for missing mandatory fields.
- Defaults include password and region constants.

## `nexis_submitter.py` (Nexis browser submitter)

### What it owns
- Nexis login.
- Navigation to employee form with fallback route logic.
- Field filling by label heuristics.
- Final submit click.

### Resilience tactics used
- Wait for `document.readyState` completion.
- Overlay removal / injection patches.
- Multiple selectors for login and navigation controls.
- JS-based clicking to bypass blocked native clicks.

## `NDISBUDGETER.py`

### What it owns
- Reads budget workbook sheets.
- Saves raw sheet backup (`Main_Agreement.csv`).
- Splits “Agreement entry” blocks into separate CSV files.

### Call patterns
- Called automatically from client budget download path.
- Can run as standalone CLI helper (`turnpoint-budgeter`).

## `purger_state.py` and `worker_state.py`

### What they own
- Thread-safe read/write access to JSON state files.
- Sequence values and purge counters.
- Last-purge metadata by entity ID.
- Capped history timeline (200 entries).

### Distinction
- Client state starts at `100001`.
- Worker state starts at `200001`.

## `combine_cleaned.py`

### What it owns
- Combines cleaned worker CSV files into aggregate raw exports.
- Also emits Nexis-formatted combined exports.

### Outputs
- Raw combined: CSV + JSON.
- Nexis combined: CSV + JSON.

## `build.py`, `turnpoint_gui.spec`, `turnpoint_cli.spec`

### What they own
- Build-time packaging with PyInstaller.
- OS-specific `dist/` layout.
- Bundling of `assets/` and required hidden imports.

## `Declutter.py`

### What it owns
- Cleanup of build outputs and temp clutter.
- Optional dry-run mode.

## `selenium_helpers.py`

### What it owns
- Generic retry wrapper.
- Wait helper.
- JS click helper.

## Tests
- `tests/test_appointment_item_discovery.py`
- `tests/test_package_collector.py`
- `tests/test_purgeable_helpers.py`

These tests focus on helper behavior and schema/diagnostic contracts; browser automation remains manual/smoke validated.

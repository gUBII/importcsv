# Configuration, Environment, and Paths

## Runtime prerequisites
- Python 3.10+ (project declares `>=3.10`; docs suggest 3.11+ for development).
- Chrome installed locally for Selenium-driven workflows.
- Valid TurnPoint credentials.
- Optional Nexis credentials for upload automation.

## Environment variables

### TurnPoint credentials and operator context
- `TP_USERNAME`: TurnPoint login email/username.
- `TP_PASSWORD`: TurnPoint login password.
- `TP_OPERATOR`: Optional default operator codename.

### Contact + archive roots
- `PURGER_CONTACT_EMAIL`: Displayed in UI contact label.
- `PURGED_ARCHIVE_ROOT`: Overrides client archive root (default `~/PurgedClients`).
- `PURGED_WORKER_ROOT`: Overrides worker archive root (default `~/PurgedWorker`).
- `LINE_ITEM_RATES_ROOT`: Overrides ServiceType truth root (default `~/LineItemRates`).

### Package-discovery and PDCC
- `PDCC_ROOT`: Overrides package discovery and bundle root.
- `PURGEABLE_CLIENTS_URL`: Overrides default purgeable clients page URL.

### Nexis credentials
- `NEXIS_USERNAME`: Optional Nexis login username.
- `NEXIS_PASSWORD`: Optional Nexis login password.

## Default storage paths

### Client outputs
- Root: `~/PurgedClients` unless overridden.
- Duplicate reports: `~/PurgedClients/_duplicate_reports`.
- Package discovery root (PDCC default):
  - `~/Purged Client/Package Divided Client Credential (PDCC)`

### Worker outputs
- Root: `~/PurgedWorker` unless overridden.
- Download cache: `~/PurgedWorker/_downloads`.

### ServiceType truth outputs
- Root: `~/LineItemRates/ServiceTypeTruth` unless overridden by `LINE_ITEM_RATES_ROOT`.
- Variants latest:
  - `variants/latest/ServiceTypeVariants_latest.csv`
  - `variants/latest/ServiceTypeVariants_latest.xlsx`
- Extraction route requirement:
  - TP1 client details `Appointments` -> `Add Appointment` nested iframes (`appointment-edit.asp` outer + Assist inner iframe with `has_parent=true`)
  - Direct Assist URL without parent/client context is unsupported for variants extraction
- Variants diagnostics:
  - `variants/diagnostics/<run_id>/events.jsonl`
  - `variants/diagnostics/<run_id>/checkers.csv`
  - HTML/PNG/console artifacts in the same directory
- Variants checkpoints:
  - `variants/checkpoints/checkpoint_<run_id>.json`
  - `variants/checkpoints/variants_append_<run_id>.csv`

### Persistent state
- Client state: `~/.turnpoint_purger/purger_state.json`
- Worker state: `~/.turnpoint_purger/worker_state.json`

## Important generated files

### Client pipeline
- `latest_purgeable_clients.xlsx`
- `package_manifest.csv`
- Per-package exports: `<Package>/<package>_clients.xlsx` and `.csv`
- Per-client archive CSV files + document folder + budget exports

### Worker pipeline
- `worker_manifest.csv`
- `latest_workers.xlsx`
- Per-worker `WorkerDetail.csv`, `Qualification.csv`, `Allowance.csv`

### ServiceType variants CSV/XLSX schema highlights
- Parent fields: `Parent Service Type ID`, `Parent Service Type Label`
- Alias fields for TruthView import: `Parent Service Type`, `Service Type ID`, `Item Number`
- Variant fields: `Service Variant Label`, `Rate`, `Rate (Raw)`, `Code`, `Code (Raw)`, `Unit`
- Operational fields: `Status`, `Error Reason`, `Conflict`, `Conflict Detail`, `Probe Client ID`, `Source URL`, `Captured At (UTC)`

### Nexis pipeline
- `combined_workers_nexis.csv`
- `combined_workers_nexis.json`
- Combined raw worker exports from `combine_cleaned.py`

## CSV contracts and expected columns

### Client batch manifest input
From `client_manifest.example.csv`:
- `client_id`
- `client_name`
- `package`

Accepted aliases in code for id/name fields include `turnpoint_id`, `client`, and `name`.

### Package manifest output
Written by package crawler with columns:
- `Order`
- `Package`
- `Client ID`
- `Client Name`
- `Details URL`

### Worker manifest output
Written by worker collector with columns:
- `Order`
- `Worker ID`
- `Full Name`
- `Team`
- `Details URL`

## State file shape (conceptual)

### Client state (`purger_state.json`)
- `next_universal_id`
- `purged_count`
- `clients` map keyed by client id
- `history` list (capped)

### Worker state (`worker_state.json`)
- `next_universal_id`
- `purged_count`
- `workers` map keyed by worker id
- `history` list (capped)

## Operational path dependencies
- Many flows rely on deterministic file naming (`*Client-Details.csv`, `*WorkerDetail.csv`).
- Combine/export utilities infer behavior from file suffix patterns.
- Running commands from unusual directories can affect auto-detection logic (notably budget helper path scanning).

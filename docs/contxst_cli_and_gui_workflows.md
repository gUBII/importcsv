# CLI and GUI Workflows

## CLI workflows (`importcsv.py`)

### Single client purge
```bash
python importcsv.py <client_id>
```
Optional flags:
- `--client-name`
- `--headless`
- `--force-duplicate`
- `--no-duplicate-prompt`

### Batch purge by package(s)
```bash
python importcsv.py --manifest clients.csv --package "Core Supports" --package "SIL"
```

### Batch purge all clients in manifest
```bash
python importcsv.py --manifest clients.csv --all-clients
```

### Purgeable snapshot download
```bash
python importcsv.py --find-purgeable
```
Optional:
- `--purgeable-url <custom_url>`
- `--headless`

### Build package bundles from purgeable workbook
```bash
python importcsv.py --bundle-download
```
Optional:
- `--bundle-package "HCP L1"` (repeatable)
- `--update-bundle` to refresh and overwrite

### Package manifest crawl
```bash
python importcsv.py --collect-packages
```
Optional:
- `--package "NDIS - Plan Managed"` (repeatable / comma-separated)
- `--manifest <output_manifest_path>`

## Worker workflows (`worker_purger.py` via UI)
Worker collection and purge are exposed primarily through the GUI, but core functions support script-level invocation:
- `collect_workers(...)`
- `download_worker_excel(...)`
- `run_worker_purge(...)`
- `run_worker_batch(...)`

## GUI workflows (`turnpoint_purger_ui.py`)

### Tab: Client Purger
Main actions:
- Set credentials.
- Enter single client ID and run purge.
- Reset client archives and counters.
- Collect package manifest.
- Find purgeable clients.
- Bundle download / update bundles.
- Purge all clients in manifest with cooldown.
- Refresh Client Atlas table.

Behavior details:
- Discovery controls are hidden until credentials exist.
- Purge-all applies minimum cooldown of 20 seconds.
- Cooldown can be overridden mid-cycle.
- Atlas rows are color-tagged by purge state.

### Tab: Worker Purger
Main actions:
- Enter worker ID and run single worker purge.
- Reset worker archives and counters.
- Collect worker manifest.
- Download worker Excel snapshot.
- Purge all workers from manifest with cooldown and override.
- Refresh Worker Atlas table.

### Tab: NexisUploader
Main actions:
- Scan workers from `PurgedWorker` root.
- Preview mapped JSON payload.
- Upload selected worker to Nexis.
- Combine worker payloads into aggregate CSV/JSON.
- Export client data from `PurgedClients` into `clients-data.csv`.

## UI thread and status model
- Actions launch background threads.
- UI state is updated by queued callbacks (`after`).
- Completion pops info/error dialogs and refreshes sequence counters.

## Suggested operator runbook
1. Set TurnPoint credentials.
2. Collect package manifest.
3. Run bundle discovery/update if needed.
4. Verify Client Atlas rows.
5. Purge selected client(s) or run purge-all.
6. Optionally switch to worker tab and repeat collection/purge.
7. Use Nexis tab for transformation/upload tasks.

## Failure handling patterns users should expect
- Duplicate clients are skipped unless overridden.
- Duplicate workers are skipped by runtime error.
- Individual extraction failures are logged; whole run may still complete.
- Manifest missing/empty errors are surfaced before purge-all starts.

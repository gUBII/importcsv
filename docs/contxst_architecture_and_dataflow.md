# Architecture and Dataflow

## Architectural style
The codebase is procedural and module-based:
- Browser automation logic lives in Python functions.
- Persistent counters and history live in JSON files.
- The GUI orchestrates backend functions in worker threads.
- Most modules interact via direct imports (no service layer).

There is no external database, API server, or message queue.

## Core component graph

```mermaid
flowchart TD
    UI["turnpoint_purger_ui.py (Tkinter)"] --> ClientCore["importcsv.py"]
    UI --> WorkerCore["worker_purger.py"]
    UI --> NexisMap["nexis_uploader.py"]
    UI --> NexisSubmit["nexis_submitter.py"]

    ClientCore --> ClientState["purger_state.py (JSON)"]
    ClientCore --> Budget["NDISBUDGETER.py"]
    WorkerCore --> WorkerState["worker_state.py (JSON)"]
    WorkerCore --> SeleniumHelpers["selenium_helpers.py"]

    ClientCore --> TP["TurnPoint (Selenium)"]
    WorkerCore --> TP
    NexisSubmit --> Nexis["Nexis365 (Selenium)"]

    ClientCore --> ClientFS["PurgedClients + PDCC folders"]
    WorkerCore --> WorkerFS["PurgedWorker folder"]
    NexisMap --> CleanedFS["CLEANEDFORNEXIS / combined exports"]
```

## Client pipeline dataflow (`importcsv.py`)

### Phase 1: Session preparation
- Validate duplicate policy (`guard_against_duplicate`).
- Reserve sequence slot (`reserve_universal_sequence`).
- Build target directories.
- Build Chrome with download preferences.

### Phase 2: TurnPoint extraction
- Login once.
- Visit each target page URL.
- Extract records using page-specific parser.
- Write one CSV per logical page.

### Phase 3: File download
- Document tab:
  - Open each document link in new tab.
  - Click download button via JS.
  - Wait for finished file.
  - Move/rename into documents folder.
- Budget tab:
  - Trigger workbook export.
  - Rename workbook with sequence prefix.
  - Optionally parse workbook into entry CSVs.

### Phase 4: Finalization
- Rename working folder using discovered client name.
- Calculate archive size.
- Persist purge event to state JSON.
- Emit logs and return final path.

## Package discovery + bundle flow

### Purgeable snapshot (`find_purgeable_clients`)
- Navigate to purgeable client URL (default/env/CLI override).
- Set page size (target 10,000 rows).
- Apply purgeable filter.
- Trigger Excel export.
- Store timestamped copy and `latest_purgeable_clients.xlsx` alias.

### Bundle export (`bundle_package_download`)
- Load latest workbook (or refresh first).
- Identify package column.
- Export one CSV + one XLSX per package to PDCC subfolders.

### Manifest crawl (`collect_clients_by_package`)
- Iterate package filter values.
- Trigger search.
- Parse client links with `eid` values.
- De-duplicate client IDs across packages.
- Persist `package_manifest.csv` with deterministic row order.

## Appointment discovery dataflow (`appointment_item_discovery.py`)

### Route resolution
- Login using shared `importcsv.py` auth helpers.
- Attempt Assist bridge route first:
  - `https://tp1.com.au/appointments-all.asp`
  - redirect into `https://assist.turnpoint.co/appointments`
  - navigate to `https://assist.turnpoint.co/appointments/new`
- If Assist route is unavailable or stalls at auth/login, fallback to legacy appointment URLs.

### Assist extraction path
- Verify page readiness (`Appointment Details - New`).
- Probe `client_id` and `service_type_id` React comboboxes with checker instrumentation.
- Treat empty option sets as warn+continue (`CHK_DROPDOWN_EMPTY`) and capture artifacts.
- Extract option rows as `{label, value}` where `value` is used as `Service Type ID`.

### Item-number enrichment path
- For each discovered `Service Type ID`, open `service-type-details.asp?eid=<id>`.
- Parse:
  - `ef581` -> item number / service code
  - `ef592` -> default rate
- Cache per-`eid` lookups to avoid repeated page fetches.

### Merge path
- Merge discovery output with `ServiceTypes_latest.csv`.
- Match precedence:
  - exact `Service Variant Label` to `Service Type`
  - fallback by `Service Type ID` to `ID`
- Rate precedence:
  - discovery payload/details-derived rate first
  - fallback to reference `Def. Rate`

## Worker pipeline dataflow (`worker_purger.py`)

### Discovery
- Open carers page.
- Expand search options.
- Set high record limit.
- Trigger search.
- Parse worker links + team labels.
- Save `worker_manifest.csv`.

### Purge
- Check duplicate from worker state.
- Reserve worker sequence ID.
- Open `user-edit.asp?eid={worker_id}`.
- Scrape fields, checkboxes, and radio groups.
- Build normalized worker payload.
- Write three CSV outputs:
  - `WorkerDetail.csv`
  - `Qualification.csv`
  - `Allowance.csv`
- Persist worker event to state JSON.

## Nexis bridge dataflow

### Mapping (`nexis_uploader.py`)
- Discover `*WorkerDetail.csv` files.
- Build `WorkerRecord` objects.
- Transform to Nexis payload fields.
- Apply defaults and normalization rules.

### Submission (`nexis_submitter.py`)
- Login to Nexis.
- Navigate to employees form (direct + fallback path).
- Fill fields by label lookup.
- Click save/submit.

### Batch combine (`combine_cleaned.py`)
- Merge raw worker CSV files.
- Emit combined raw CSV/JSON.
- Emit combined Nexis-formatted CSV/JSON.

## Cross-cutting concerns

### Logging
- `importcsv.log_message` is the shared logger.
- UI injects a log sink callback to stream live output into the text area.
- Appointment discovery additionally writes structured diagnostics per run:
  - `events.jsonl` (event schema with code/step/context)
  - `checkers.csv` (pass/fail/warn checkpoints)
  - `summary.json` (aggregate counters and output paths)

### State and idempotency
- Client duplicates are explicit and can be overridden.
- Worker duplicates are currently hard-stop skips.
- Sequence IDs are monotonic and persisted.

### Error handling style
- Long-running pipelines use broad exception guards to keep loops alive.
- Most failures are logged and surfaced to UI dialogs.
- Extraction often continues even if one page fails.

## Storage topology
- Client archives: `~/PurgedClients` (or `PURGED_ARCHIVE_ROOT`).
- Worker archives: `~/PurgedWorker` (or `PURGED_WORKER_ROOT`).
- Client package/bundle assets: PDCC path under home directory unless overridden.
- State files:
  - `~/.turnpoint_purger/purger_state.json`
  - `~/.turnpoint_purger/worker_state.json`

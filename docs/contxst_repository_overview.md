# Repository Overview

## Executive summary
This repository automates TurnPoint and Nexis operations with filesystem-first outputs. It now has four practical data pipelines:
- Client purge + archival (`importcsv.py`).
- Worker purge + archival (`worker_purger.py`).
- Nexis mapping/submission (`nexis_uploader.py`, `nexis_submitter.py`).
- Service Type variant truth extraction from Assist appointment editor (`appointment_item_discovery.py`).

## Core runtime modules
- `importcsv.py`: Client purge orchestration, discovery/bundle workflows, CLI parser.
- `appointment_item_discovery.py`: Assist appointment-editor extraction of per-Service-Type variant rows (Service, Rate, Code, Unit), with strict readiness checks, selector fallbacks, and diagnostics artifacts.
- `service_type_rate_extractor.py`: Service Types reference extraction from `service-types.asp` (reference/enrichment source).
- `turnpoint_purger_ui.py`: Tkinter UI for client/worker purge and ServiceType reference tab.
- `worker_purger.py`: Worker discovery and purge workflows.
- `nexis_uploader.py`: Worker mapping to Nexis payload.
- `nexis_submitter.py`: Selenium submitter for Nexis form workflow.
- `purger_state.py`, `worker_state.py`: JSON-backed counters/history.

## Primary capabilities

### 1) Client purge and archival
- Extracts multiple client pages.
- Downloads documents and budgets.
- Writes CSVs and archive folders under sequential IDs.
- Records purge history and duplicate metadata.

### 2) PDCC discovery workflows
- `--find-purgeable`: downloads purgeable workbook.
- `--bundle-download` / `--update-bundle`: package CSV/XLSX outputs.
- `--collect-packages`: crawls package filters and writes `package_manifest.csv`.

### 3) Service Type variant extraction (Assist)
- Opens appointment editor only via TP1 client details -> `Appointments` -> `Add Appointment`, then switches nested iframes:
  - outer iframe: `iframe[src*='appointment-edit.asp']`
  - inner iframe: `iframe[src*='assist.turnpoint.co/appointments/new'][src*='has_parent=true']`
- Direct Assist URL without parent/client context is treated as unsupported for variants extraction.
- Applies a capability gate before extraction:
  - `div[data-cy='service_type_id-reload']` exists
  - `//div[@data-cy='service_type_id-reload']/preceding::input[@role='combobox'][1]` is interactable
  - sentinel selection yields `day_rate-input` + `day_code-input`
- For each Service Type:
  - selects combobox option with deterministic retries (type + Enter, then exact option click)
  - waits for stale-guard fingerprint change before reading values
  - extracts fixed six variant pairs from `input[data-cy='{prefix}_rate-input']` and `input[data-cy='{prefix}_code-input']`
  - captures `Service Variant Label`, `Rate`, `Code`, and `Unit` with raw + normalized values
- On per-Service-Type failures, writes a `Status=FAIL` row with `Error Reason`, captures artifacts, and continues to the next Service Type.

### 4) Variant outputs and diagnostics
- Outputs are written to `~/LineItemRates/ServiceTypeTruth/variants/`:
  - `latest/ServiceTypeVariants_latest.csv`
  - `latest/ServiceTypeVariants_latest.xlsx`
  - `snapshots/ServiceTypeVariants_<run_id>.csv/.xlsx`
- Rows include TruthView alias columns in addition to debug fields:
  - `Parent Service Type`
  - `Service Type ID`
  - `Item Number`
- Multi-probe conflicts are surfaced with `Conflict` + `Conflict Detail`.
- XLSX parent columns (`Parent Service Type ID`, `Parent Service Type Label`) are merged across contiguous variant rows.
- Diagnostics are written per run under:
  - `~/LineItemRates/ServiceTypeTruth/variants/diagnostics/<run_id>/events.jsonl`
  - `~/LineItemRates/ServiceTypeTruth/variants/diagnostics/<run_id>/checkers.csv`
  - HTML/PNG/console artifacts for open/select/extract failures.

## Runtime model
The project is procedural, Selenium-driven, and heavily file-based. It has no external API server or database; local JSON and CSV/XLSX outputs are the operating state.

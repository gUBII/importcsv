# Repository Overview

## Executive summary
This repository automates TurnPoint and Nexis operations with filesystem-first outputs. It now has four practical data pipelines:
- Client purge + archival (`importcsv.py`).
- Worker purge + archival (`worker_purger.py`).
- Nexis mapping/submission (`nexis_uploader.py`, `nexis_submitter.py`).
- Appointment-driven item-number discovery + service-type enrichment (`appointment_item_discovery.py`).

## Core runtime modules
- `importcsv.py`: Client purge orchestration, discovery/bundle workflows, CLI parser.
- `appointment_item_discovery.py`: Assist-first appointment probing, React combobox extraction, details-page item-number enrichment, deep diagnostics, and merge with service-type reference export.
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

### 3) Appointment-driven item discovery
- Attempts Assist route first (`appointments-all` -> Assist -> `/appointments/new`), then falls back to legacy pages when Assist is unavailable.
- Validates checker chains for both modes:
  - Assist: `CHK_ROUTE_ASSIST_REACHED`, `CHK_APPOINTMENT_NEW_REACHED`, `CHK_CLIENT_COMBO_PRESENT`, `CHK_CLIENT_OPTIONS_NONEMPTY`, `CHK_SERVICE_COMBO_PRESENT`, `CHK_SERVICE_OPTIONS_NONEMPTY`.
  - Legacy: `CHK_CLIENT_PAGE_OPEN`, `CHK_APPOINTMENT_ENTRY_REACHED`, `CHK_DROPDOWN_PRESENT`, `CHK_DROPDOWN_VISIBLE_ENABLED`, `CHK_DROPDOWN_OPTIONS_EXTRACTED`, `CHK_DROPDOWN_NONEMPTY`.
- Empty/missing states emit `CHK_DROPDOWN_EMPTY` as warn+continue and always capture screenshot + HTML diagnostics.
- Discovery output keeps `Item Number` as primary field and `Service Code` as compatibility alias.

### 4) ServiceType enrichment
- For each discovered `Service Type ID`, opens `service-type-details.asp?eid=<id>` and extracts:
  - `ef581` -> `Item Number` / `Service Code`
  - `ef592` -> `Rate` with `Rate Source=service_type_details` when applicable.
- Merges discovery rows with `ServiceTypes_latest.csv`.
- Match precedence: exact label first, then service-type ID.
- Rate precedence: payload rate first, fallback to `Def. Rate` with `fallback_service_types` source.

## Runtime model
The project is procedural, Selenium-driven, and heavily file-based. It has no external API server or database; local JSON and CSV/XLSX outputs are the operating state.

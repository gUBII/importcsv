# Repository Overview

## Executive summary
This repository is an automation toolkit for operations teams working in TurnPoint and Nexis365. Its main objective is to reduce manual data handling by automating extraction, conversion, and packaging of client and worker records.

At a high level, the system:
- Logs into TurnPoint via Selenium.
- Extracts structured pages into CSV exports.
- Downloads linked files (documents, budget spreadsheets, workbook snapshots).
- Writes archives into sequentially numbered folders.
- Tracks purge history and duplicate guards with local JSON state.
- Provides a Tkinter desktop UI for operators.
- Provides CLI workflows for batch jobs and discovery tasks.
- Converts worker records into Nexis-compatible payloads.

## What the repository contains

### Core runtime
- `importcsv.py`: Main client-purge automation engine and CLI entry.
- `turnpoint_purger_ui.py`: Tkinter UI shell for client/worker purge and Nexis tools.
- `worker_purger.py`: Worker-specific purge engine.
- `NDISBUDGETER.py`: Budget workbook parser and CSV exporter.
- `purger_state.py`: Client purge state persistence.
- `worker_state.py`: Worker purge state persistence.
- `nexis_uploader.py`: Worker CSV to Nexis payload mapping.
- `nexis_submitter.py`: Selenium-driven Nexis form submission.
- `combine_cleaned.py`: Batch combine utility for cleaned worker files.

### Build and distribution
- `pyproject.toml`: Project metadata and script entry points.
- `build.py`: Wrapper for PyInstaller builds.
- `turnpoint_gui.spec`: GUI bundle spec.
- `turnpoint_cli.spec`: CLI bundle spec.
- `requirements-build.txt`: Build-only dependencies.

### Tooling and hygiene
- `Declutter.py`: Removes build artifacts, caches, and clutter files.
- `tests/`: Lightweight unit tests around selected helper behavior.

### Assets and sample data
- `assets/`: UI images/GIFs and sample exports.
- `client_manifest.example.csv`: Batch-manifest template.
- `FormatforClient(Nexis)/clients-data.csv`: Client export output target.

## Primary user-facing capabilities

### 1) Client purge and archival
For each TurnPoint client ID:
- Pulls details across multiple pages.
- Writes normalized CSV snapshots.
- Downloads related documents.
- Downloads and optionally parses NDIS budget workbook.
- Saves everything into a sequential archive folder.
- Updates state counters and duplicate history.

### 2) Client package discovery and bundling
- Downloads “purgeable” client workbook.
- Discovers package names from workbook columns.
- Exports package-specific CSV/XLSX bundles.
- Crawls TurnPoint package filters to build a manifest CSV used by UI tables and purge-all runs.

### 3) Worker purge and archival
- Collects worker roster manifest from TurnPoint carers page.
- Scrapes worker edit form fields.
- Writes worker detail, qualification, and allowance CSV files.
- Archives under independent worker sequence counters.

### 4) Nexis bridge
- Reads worker CSV archives.
- Maps fields into Nexis employee payload schema.
- Shows JSON previews in UI.
- Can submit one selected payload to Nexis through Selenium.
- Supports combined worker exports (CSV + JSON) for downstream operations.

## Runtime model in one sentence
This is a filesystem-first, Selenium-driven, stateful operations toolchain with two main extraction pipelines (client + worker) and one transformation/upload pipeline (Nexis).

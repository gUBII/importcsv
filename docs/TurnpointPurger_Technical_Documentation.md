# TurnpointPurger Automation Suite – Technical Documentation

## Title Page
- **Project:** TurnpointPurger Automation Suite  
- **Version:** 2.0.1  
- **Scope:** Python + Selenium stack that automates TurnPoint (tp1.com.au) data extraction, budgeting exports, and archival.

## Executive Summary
TurnpointPurger is a Python automation toolkit that logs into the TurnPoint portal, scrapes all major client artifacts (details, schedules, notes, support/emergency plans, documents, NDIS budgets), downloads attachments, and normalizes the output into CSV/Excel-based archives under sequential “NexisID” folders. A Tkinter GUI wraps the automation with live logs, credential management, and purge history, while a CLI version can run headless. State is persisted locally in JSON so each client receives a unique universal prefix and duplicate purges can be detected.

## Architecture Overview
| Module | Role | Notes |
| --- | --- | --- |
| `importcsv.py` | Core automation pipeline. | Manages state, Selenium driver creation, navigation, scraping, downloads, CSV writing, and archival. |
| `turnpoint_purger_ui.py` | Tkinter desktop UI. | Drives the pipeline, displays status, collects credentials, and exposes reset/history functions. |
| `NDISBUDGETER.py` | Budget helper. | Pandas-based parser that converts TurnPoint Excel budgets into entry CSVs. |
| `purger_state.py` | Persistence layer. | Thread-safe JSON storage for purge counters, history, duplicates. |
| `build.py` / `turnpoint_*.spec` | Packaging. | PyInstaller helpers for macOS/Windows GUI + CLI. |
| Assets & docs | Branding + references. | Includes README, packaging notes, GIF assets. |

The architecture is modular/procedural (not a strict Page Object Model). Helper functions are grouped by responsibility (state, scraping, downloads) and orchestrated by `run_turnpoint_purge`.

## Functional Breakdown

### `importcsv.py`
| Component | Description |
| --- | --- |
| **Global config** | Loads `.env` (TP credentials, contact e-mail, optional archive root). Defines defaults and shared globals (`CLIENT_ID`, `FILE_PREFIX`, directories). |
| **Logging** | `set_log_sink`, `log_message` send log lines to stdout or the GUI queue. |
| **Operator state** | `prompt_operator_name`, `set_operator_name` annotate logs with operator code names. |
| **Duplicate handling** | `get_client_last_purge`, `create_duplicate_report`, `confirm_duplicate_cli` consult JSON history and warn before rerunning the same client. |
| **Duplicate gate** | `guard_against_duplicate` enforces the duplicate policy (raise, prompt, or override) before a purge reserves the next NexisID. |
| **Purgeable discovery** | `find_purgeable_clients`, `_download_purgeable_clients_excel`, `_discover_packages_from_dataframe` force the TurnPoint search limit to 10k, apply purgeable filters, download the Excel dataset, and persist it to `PDCC/latest_purgeable_clients.xlsx`. |
| **Package bundles** | `bundle_package_download` + `_export_package_dataframe` convert the purgeable workbook into per-package Excel/CSV pairs under `Purged Client/Package Divided Client Credential (PDCC)/<Package>/`. Supports bundle refresh (`refresh/update` flag) and package subsets. |
| **Credentials** | `configure_credentials`, `ensure_credentials`, runtime globals allow the GUI to override `.env` values. |
| **Archive management** | `assign_universal_sequence`, `ensure_archive_root`, `configure_client_context`, `update_final_client_name`, `finalize_output_directory`, `cleanup_old_csvs`, `reset_purge_data`, `calculate_directory_bytes` manage folder structure, sequential numbering, rename fallback (copytree on cross-device operations). |
| **Selenium login** | `login(driver)` navigates to `BASE_URL`, waits for the login form, submits credentials, and waits for `/dashboard` via `WebDriverWait`. |
| **DOM field scraping** | `extract_fields_on_page` collects labels and adjacent inputs/values via composite XPaths; handles selects, inputs, textareas, sibling tables, and deduplicates labels. |
| **CSV writer** | `write_csv` guarantees consistent headers across records for each page. |
| **Downloads** | `snapshot_downloads`, `wait_for_new_download`, `download_document_files` open document links in new windows via JS, scroll to download buttons, click via `execute_script`, poll the folder for completion, sanitize names, and move into `Documents/`. `download_budget_excel` follows a similar flow then calls `process_budget_excel`. |
| **Page extractors** | `extract_client_details`, `extract_package_schedules`, `extract_notes`, `extract_info_sheet`, `extract_agreement`, `extract_contacts`, `extract_support_plan`, `extract_emergency_plan`, `extract_documents`, `extract_ndis_budget` each visit a dedicated URL, wait for anchor elements, scrape data, and optionally trigger downloads. Notes parsing splits multi-line cells into structured records. |
| **WebDriver factory** | `build_chrome_driver(headless)` configures Chrome options (set download dir, disable prompts, headless flags, sandbox mitigations). |
| **Orchestration** | `run_turnpoint_purge(client_id, client_name=None, headless=False)` reserves a universal ID, configures directories, builds drivers, logs in, iterates across extractors, writes CSV outputs, renames archives once better names are discovered, handles downloads, computes archive size, records events, logs summaries, and returns the final path. Always quits the driver and finalizes the output directory. |
| **CLI entry** | `main()` prompts for the client ID and launches the purge. |
| **Batch CLI helpers** | `parse_cli_args`, `load_client_manifest`, `select_clients_by_packages`, `run_client_batch` power the new manifest-driven workflows (per-package batches or full 260+ client sweeps). |
| **Purgeable CLI flags** | `--find-purgeable`, `--bundle-download`, `--update-bundle`, and `--bundle-package` expose the PDCC dataset download + bundle exports without invoking per-client purges. |

**Selenium commands used**
- `driver.get(url)`
- `find_element(...).send_keys()` / `.click()`
- `driver.execute_script(...)` for tab creation, scrolling, clicking, and retrieving values.
- `driver.switch_to.window`
- `WebDriverWait(...).until(EC.*)` for login page, anchor elements, clickable download buttons.
- Polling of the download directory to skip `.crdownload` artifacts.

### `turnpoint_purger_ui.py`
- Subclasses `tk.Tk`; defines styles, frames, neon progress bars, GIF animation.
- Uses `queue.Queue` to marshal logs from the core script (`set_log_sink`).
- Wraps the entire dashboard inside a full-screen, scrollable canvas so all controls/logs remain accessible regardless of monitor size.
- Widgets include a visual panel with a “Powered by Nexix365” badge and circular GIF (Pillow `ImageSequence` + cropping), directive console for credentials/controls, and a log panel with a scrolled text console and ASCII signature.
- Client discovery controls add **Find Purgeable Clients**, **Bundle Download (All Packages)**, and **Update package bundle to latest** buttons; the entire section is hidden until credentials are configured so bundle jobs cannot run anonymously. Each button spawns a background worker that calls the new `importcsv` helpers, logs results, and pops message boxes on completion/errors.
- Event handlers:
  - `_handle_engage` spawns a background thread to call `run_turnpoint_purge`.
  - `_execute_purge` wraps the call, enqueues success/failure messages, displays message boxes, and refreshes counters.
  - `_handle_reset_purge` confirms and calls `reset_purge_data`.
  - `_handle_set_credentials` opens a modal dialog, collects credentials, validates non-empty values, calls `configure_credentials`, updates display, logs the change.
  - `_prompt_operator_name` collects an operator codename and calls `set_operator_name`.
  - `_refresh_sequence_stats`, `_refresh_credential_display`, `_drain_log_queue`, `_set_running`, `_load_profile_animation`, `_animate_profile_gif` keep UI state synchronized.
- GUI uses `messagebox.showinfo/showerror` for status feedback and `after()` timers to update animations/log drains.

### `NDISBUDGETER.py`
- `auto_detect_excel_file()` scans recent NexisID folders (and root) for `.xlsx/.xls`, biased toward the last processed ID in `purger_state`.
- `process_budget_excel()` reads workbooks via pandas/openpyxl, writes raw CSV backups, splits “Agreement entry” blocks into per-day CSV files under `Entries/`, and returns a summary dict.
- `generate_budget_exports()` offers a CLI helper to auto-detect spreadsheets, prompt the user, and invoke `process_budget_excel`.

### `purger_state.py`
- Manages JSON persistence under `~/.turnpoint_purger/purger_state.json`.
- Stores `next_universal_id`, `purged_count`, per-client metadata, duplicate history, and a capped `history` list (200 items).
- APIs: `get_purge_statistics`, `reserve_universal_sequence`, `get_client_last_purge`, `get_recent_history`, `record_purge_event`, `reset_state`.
- `record_purge_event` increments counters, stores bytes/timestamps/operators, and keeps `next_universal_id` monotonic.

### Packaging (`build.py`, specs)
- `build.py` installs PyInstaller if missing and runs GUI/CLI specs with platform-specific dist paths.
- `turnpoint_gui.spec` bundles the UI, includes `assets/`, hidden imports (NDISBUDGETER/importcsv/Pillow), and emits both `.exe` and `.app`.
- `turnpoint_cli.spec` bundles the CLI entry.

### Other Assets
- `README.md`, `PACKAGING.md`, requirements, and branding assets that appear inside the UI and docs.

## JavaScript & DOM Interaction Mapping
- `window.open(...)` opens each document detail page in new tabs for downloading (avoids modal/target constraints).
- `scrollIntoView(true)` ensures clickable buttons are visible before Selenium interacts.
- `arguments[0].click()` bypasses overlay/pop-up issues that block native `.click()`.
- Combined with `WebDriverWait(... element_to_be_clickable)` to confirm DOM readiness before executing JS, minimizing flaky interactions.

## Dependency & Integration Mapping
- **Python stdlib:** `os`, `time`, `shutil`, `pathlib`, `csv`, `json`, `threading`, `queue`, `datetime`.
- **Third-party:** `selenium`, `python-dotenv`, `pandas`, `openpyxl`, `tkinter`, `Pillow`, `PyInstaller`.
- **External system:** TurnPoint web portal (`https://tp1.com.au`). Interactions occur exclusively via browser automation (no REST API).
- **Credential flow:** `.env` → `TP_USERNAME`/`TP_PASSWORD`; GUI updates via `configure_credentials`.
- **Downloads:** Chrome configured with `download.default_directory`, files renamed/sanitized once complete.
- **Data persistence:** `PurgedClients/<NexisID Client>` for working folders, `_duplicate_reports/`, and `~/.turnpoint_purger/purger_state.json`.
- **Purgeable dataset config:** `PURGEABLE_CLIENTS_URL` (env var) overrides the default `https://tp1.com.au/client-list.asp?purgeable=yes`; CLI flag `--purgeable-url` provides per-run overrides without touching `.env`.

## Database / Storage
- No database; relies on filesystem storage.
- Working folder: `PurgedClients/<NexisID ClientID>/` containing CSVs, downloaded documents, and `NDIS_Budget_Exports/`.
- Final folder renamed to include client names (`<NexisID CLIENT NAME (ID)>`); fallback copytree handles cross-device/permission issues.
- Package exports: `Purged Client/Package Divided Client Credential (PDCC)/` houses the purgeable workbook (`latest_purgeable_clients.xlsx`) plus per-package folders with `<Package Name>_clients.xlsx` + `.csv`.
- JSON state file structure:
  ```json
  {
    "next_universal_id": 100003,
    "purged_count": 2,
    "clients": {
      "56851": {
        "universal_id": 100001,
        "client_name": "KHAIR Adam",
        "bytes": 1234567,
        "timestamp": "2024-11-12T13:45:01.234567+00:00",
        "operator": "Farhan"
      }
    },
    "history": [ ... up to 200 entries ... ]
  }
  ```

## Automation Workflow Sequence (textual)
1. `run_turnpoint_purge` reserves the next universal ID and configures directories.
2. Credentials resolved via `.env` and optional GUI overrides.
3. Chrome driver built (`headless` flag optional) with download directory configured.
4. Driver navigates to TurnPoint, executes login, and waits for dashboard confirmation.
5. Iterates through `pages_and_extractors`, each of which:
   - Navigates to the target URL.
   - Waits for a page-specific anchor element.
   - Calls `extract_fields_on_page` or document/budget download helpers.
   - Writes CSV output via `write_csv`.
6. Document and budget downloads trigger `window.open`, scroll/click automation, and filesystem polling until files land in `Documents/` or `NDIS_Budget_Exports/`.
7. Once a better client name is discovered, directories are renamed accordingly.
8. Archive size calculated, `record_purge_event` updates JSON state, duplicate reports generated if needed.
9. Driver quits, `finalize_output_directory` confirms final naming/copying, and the CLI/GUI returns success status and path.

## Identified Issues / Code Smells
- Heavy reliance on global mutable state in `importcsv.py` complicates concurrency and testing.
- Lack of page objects centralizing selectors increases maintenance cost when TurnPoint DOM changes.
- Hard-coded wait durations in download polling/`WebDriverWait` may be brittle on slow networks.
- Broad `except Exception` blocks swallow details that could aid debugging (e.g., login failures).
- Cross-device rename fallback handles generic `OSError` but still needs consistent handling for `PermissionError` variants.
- Credential validation only happens at runtime; GUI cannot verify ahead of a purge.
- GUI window is non-resizable, which impairs usability on smaller or scaled displays.

## Recommendations
1. Encapsulate selectors and actions into lightweight page object helpers or constants to localize DOM dependencies.
2. Introduce centralized wait configuration with retry/backoff to improve resilience on slow networks.
3. Adopt structured logging (Python `logging` or JSON logs) to enrich diagnostics and facilitate remote monitoring.
4. Expand exception handling to log stack traces/context without fully suppressing errors; bubble critical failures to the UI/CLI.
5. Enhance rename fallback to explicitly handle Windows permission edge cases and log the recovery path.
6. Provide CLI flags for headless runs, custom archive roots, and verbosity; add GUI credential validation (test login).
7. Add unit tests for helpers (`calculate_directory_bytes`, `purger_state`) and integration smoke tests for extractor flows.
8. Consider GUI improvements (resizable window, DPI-aware scaling, theming options) based on user feedback.

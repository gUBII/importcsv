"""
Assist Service Type Variant Truth Extractor

Extracts variant-level data from Assist appointments form:
- All variants per Service Type (not just defaults)
- Complete Rate + Code pairs per variant
- Checkpointing for resumable extraction
- Conflict detection across probe clients

Output: ~/LineItemRates/ServiceTypeTruth/variants/{latest,snapshots}/
"""

import csv
import json
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from urllib.parse import parse_qs

from selenium.common.exceptions import TimeoutException, NoSuchElementException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import Select

from importcsv import (
    ARCHIVE_ROOT,
    BASE_URL,
    build_chrome_driver,
    ensure_credentials,
    login,
    log_message,
)
import line_item_paths
from selenium_helpers import wait_for
from openpyxl import load_workbook
from openpyxl.styles import Alignment

# =============================================================================
# TYPES & CALLBACKS
# =============================================================================

ProgressCallback = Optional[Callable[[str], None]]
EventCallback = Optional[Callable[[Dict[str, str]], None]]
RowCallback = Optional[Callable[[Dict[str, str]], None]]

EXCEL_ILLEGAL_CHAR_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")

# =============================================================================
# OUTPUT SCHEMA
# =============================================================================

VARIANT_COLUMNS = [
    "Parent Service Type ID",
    "Parent Service Type Label",
    "Service Variant Label",
    "Rate",
    "Rate (Raw)",
    "Code",
    "Code (Raw)",
    "Unit",
    "Conflict",
    "Probe Client ID",
    "Source URL",
    "Captured At (UTC)",
]

# =============================================================================
# DIAGNOSTICS FIELDS
# =============================================================================

EVENT_FIELDS = [
    "run_id",
    "ts_utc",
    "level",
    "step",
    "code",
    "message",
    "client_id",
    "url",
    "selector",
    "option_count",
    "context_json",
]

CHECKER_FIELDS = [
    "run_id",
    "ts_utc",
    "step",
    "status",
    "code",
    "message",
    "client_id",
    "url",
    "selector",
    "option_count",
    "artifact_screenshot",
    "artifact_html",
]

# Checker codes
CHECKER_CLIENT = "CHK_CLIENT_PAGE_OPEN"
CHECKER_APPOINTMENT = "CHK_APPOINTMENT_ENTRY_REACHED"
CHECKER_SERVICE_TYPE_SELECT = "CHK_SERVICE_TYPE_SELECT"
CHECKER_VARIANT_TABLE_EXTRACT = "CHK_VARIANT_TABLE_EXTRACT"
CHECKER_VARIANT_TABLE_MISSING = "CHK_VARIANT_TABLE_MISSING"
CHECKER_VARIANT_TABLE_EMPTY = "CHK_VARIANT_TABLE_EMPTY"

# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================


def _utc_now() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat() + "Z"


def _emit(message: str, callback: ProgressCallback = None):
    """Emit progress message."""
    if callback:
        callback(message)


def _normalize_text(value) -> str:
    """Normalize text: strip, collapse whitespace."""
    if not value:
        return ""
    return re.sub(r"\s+", " ", str(value).strip())


def _normalize_token(value: str) -> str:
    """Normalize token: strip, lowercase, alphanumeric + underscore."""
    text = _normalize_text(value).lower()
    return re.sub(r"[^a-z0-9_]", "", text)


def _safe_json(value) -> str:
    """Safely serialize value to JSON."""
    try:
        return json.dumps(value, default=str)
    except Exception:
        return str(value)


def _coerce_rate_text(value: str) -> str:
    """Extract and normalize numeric rate."""
    if not value:
        return ""
    # Extract leading numeric part (handles "$100.50" → "100.50")
    match = re.search(r"[\d.]+", str(value).strip())
    return match.group() if match else str(value).strip()


def _sanitize_excel_text(value) -> Tuple[str, int]:
    """Remove XLSX-illegal characters."""
    if not value:
        return "", 0
    text = str(value)
    cleaned = EXCEL_ILLEGAL_CHAR_RE.sub("", text)
    return cleaned, len(text) - len(cleaned)


# =============================================================================
# DIAGNOSTICS RECORDER
# =============================================================================


class DiagnosticsRecorder:
    """Record diagnostics events and artifacts."""

    def __init__(self, run_id: str, output_dir: Path):
        self.run_id = run_id
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.events: List[Dict[str, str]] = []
        self.checkers: List[Dict[str, str]] = []

    def event(
        self,
        level: str,
        step: str,
        code: str,
        message: str,
        client_id: Optional[str] = None,
        url: Optional[str] = None,
        selector: Optional[str] = None,
        option_count: Optional[int] = None,
        context: Optional[Dict[str, Any]] = None,
    ):
        """Record event."""
        event_dict = {
            "run_id": self.run_id,
            "ts_utc": _utc_now(),
            "level": level,
            "step": step,
            "code": code,
            "message": message,
            "client_id": client_id or "",
            "url": url or "",
            "selector": selector or "",
            "option_count": str(option_count or ""),
            "context_json": _safe_json(context) if context else "",
        }
        self.events.append(event_dict)

    def checker(
        self,
        step: str,
        status: str,
        code: str,
        message: str,
        client_id: Optional[str] = None,
        url: Optional[str] = None,
        selector: Optional[str] = None,
        option_count: Optional[int] = None,
        screenshot: Optional[Path] = None,
        html: Optional[Path] = None,
    ):
        """Record checker result."""
        checker_dict = {
            "run_id": self.run_id,
            "ts_utc": _utc_now(),
            "step": step,
            "status": status,
            "code": code,
            "message": message,
            "client_id": client_id or "",
            "url": url or "",
            "selector": selector or "",
            "option_count": str(option_count or ""),
            "artifact_screenshot": str(screenshot.name) if screenshot else "",
            "artifact_html": str(html.name) if html else "",
        }
        self.checkers.append(checker_dict)

    def save(self):
        """Save events and checkers to disk."""
        # Save events as JSONL
        events_path = self.output_dir / "events.jsonl"
        with open(events_path, "w") as f:
            for event in self.events:
                f.write(json.dumps(event) + "\n")

        # Save checkers as CSV
        checkers_path = self.output_dir / "checkers.csv"
        if self.checkers:
            with open(checkers_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=CHECKER_FIELDS)
                writer.writeheader()
                writer.writerows(self.checkers)

    def capture_screenshot(self, driver, name: str) -> Path:
        """Capture screenshot and save."""
        path = self.output_dir / f"{name}.png"
        try:
            driver.save_screenshot(str(path))
            return path
        except Exception:
            return None

    def capture_html(self, driver, name: str) -> Path:
        """Capture page HTML and save."""
        path = self.output_dir / f"{name}.html"
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(driver.page_source)
            return path
        except Exception:
            return None


# =============================================================================
# FILE I/O
# =============================================================================


def _write_csv(rows: List[Dict[str, str]], fieldnames: List[str], path: Path):
    """Write rows to CSV file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            # Sanitize Excel-illegal chars
            cleaned_row = {}
            for key, value in row.items():
                if value is not None:
                    cleaned, _ = _sanitize_excel_text(value)
                    cleaned_row[key] = cleaned
                else:
                    cleaned_row[key] = ""
            writer.writerow(cleaned_row)


def _write_xlsx(rows: List[Dict[str, str]], fieldnames: List[str], path: Path) -> Path:
    """Write rows to XLSX file."""
    import openpyxl

    path.parent.mkdir(parents=True, exist_ok=True)

    # Create workbook
    wb = openpyxl.Workbook()
    ws = wb.active

    # Write header
    for col_idx, field in enumerate(fieldnames, 1):
        ws.cell(1, col_idx).value = field

    # Write rows
    for row_idx, row in enumerate(rows, 2):
        for col_idx, field in enumerate(fieldnames, 1):
            value = row.get(field, "")
            if value is not None:
                cleaned, _ = _sanitize_excel_text(value)
                ws.cell(row_idx, col_idx).value = cleaned

    wb.save(str(path))
    return path


def _apply_xlsx_merged_cells(xlsx_path: Path):
    """Apply merged cells and formatting to XLSX file."""
    try:
        wb = load_workbook(xlsx_path)
        ws = wb.active

        # Group rows by Parent Service Type ID (column A)
        grouped = {}
        for row_idx in range(2, ws.max_row + 1):
            parent_id = ws.cell(row_idx, 1).value
            if parent_id:
                if parent_id not in grouped:
                    grouped[parent_id] = []
                grouped[parent_id].append(row_idx)

        # Merge Parent columns (A and B) for rows with same parent
        for parent_id, row_indices in grouped.items():
            if len(row_indices) > 1:
                start_row = min(row_indices)
                end_row = max(row_indices)
                # Merge Parent ID column
                try:
                    ws.merge_cells(f"A{start_row}:A{end_row}")
                except Exception:
                    pass
                # Merge Parent Label column
                try:
                    ws.merge_cells(f"B{start_row}:B{end_row}")
                except Exception:
                    pass
                # Center align merged cells
                ws.cell(start_row, 1).alignment = Alignment(vertical="center")
                ws.cell(start_row, 2).alignment = Alignment(vertical="center")

        # Freeze header row
        ws.freeze_panes = "A2"

        wb.save(str(xlsx_path))
    except Exception as e:
        log_message(f"Warning: Could not apply XLSX merges: {e}")


# =============================================================================
# CHECKPOINT MANAGEMENT
# =============================================================================


def _load_checkpoint(run_id: str) -> Optional[Dict[str, Any]]:
    """Load checkpoint JSON from disk."""
    paths = line_item_paths.get_variant_paths(run_id)
    checkpoint_path = paths["checkpoint_json"]

    if not checkpoint_path.exists():
        return None

    try:
        with open(checkpoint_path, "r") as f:
            return json.load(f)
    except Exception:
        return None


def _update_checkpoint(run_id: str, checkpoint: Dict[str, Any]):
    """Save checkpoint JSON to disk."""
    paths = line_item_paths.get_variant_paths(run_id)
    checkpoint_path = paths["checkpoint_json"]
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

    with open(checkpoint_path, "w") as f:
        json.dump(checkpoint, f, indent=2)


def _append_to_checkpoint_csv(run_id: str, rows: List[Dict[str, str]]):
    """Append rows to checkpoint CSV buffer."""
    if not rows:
        return

    paths = line_item_paths.get_variant_paths(run_id)
    csv_path = paths["checkpoint_append_csv"]
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    # Check if file exists
    file_exists = csv_path.exists()

    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=VARIANT_COLUMNS)
        if not file_exists:
            writer.writeheader()
        for row in rows:
            cleaned_row = {}
            for key, value in row.items():
                if value is not None:
                    cleaned, _ = _sanitize_excel_text(value)
                    cleaned_row[key] = cleaned
                else:
                    cleaned_row[key] = ""
            writer.writerow(cleaned_row)


# =============================================================================
# ASSIST NAVIGATION & EXTRACTION (Reused patterns)
# =============================================================================


def _open_assist_appointments_new(driver, recorder: DiagnosticsRecorder) -> bool:
    """Navigate to Assist /appointments/new."""
    try:
        url = "https://assist.turnpoint.co/appointments/new"
        driver.get(url)
        time.sleep(1)

        # Check that page loaded
        body_text = driver.page_source
        if "appointment" in body_text.lower():
            recorder.checker(
                "open_assist",
                "PASS",
                CHECKER_APPOINTMENT,
                "Appointments page loaded",
                url=url,
            )
            return True
        else:
            recorder.checker(
                "open_assist",
                "FAIL",
                CHECKER_APPOINTMENT,
                "Appointments page did not load",
                url=url,
            )
            return False
    except Exception as e:
        recorder.event(
            "ERROR",
            "open_assist",
            "EXCEPTION",
            str(e),
        )
        return False


def _find_assist_react_input(driver, container_id: str):
    """Find React input element in Assist dropdown."""
    try:
        container = driver.find_element(By.ID, container_id)
        inputs = container.find_elements(By.TAG_NAME, "input")
        if inputs:
            return inputs[0]
    except Exception:
        pass
    return None


def _collect_assist_options(
    driver, container_id: str, recorder: DiagnosticsRecorder
) -> List[Dict[str, str]]:
    """Extract options from Assist React dropdown."""
    options = []
    try:
        react_input = _find_assist_react_input(driver, container_id)
        if not react_input:
            return options

        # Click to open
        react_input.click()
        time.sleep(0.5)

        # Try to extract from visible options
        try:
            option_elements = driver.find_elements(
                By.XPATH, "//div[@role='option'] | //li[contains(@class,'option')]"
            )
            for elem in option_elements:
                text = _normalize_text(elem.text)
                # Try to get data-value or other identifier
                value = elem.get_attribute("data-value")
                if not value:
                    value = elem.get_attribute("value")
                if not value:
                    value = text  # Fallback to text
                if text and value:
                    options.append({"label": text, "value": value})
        except Exception:
            pass

        # Close by pressing Escape
        react_input.send_keys(Keys.ESCAPE)
        time.sleep(0.3)

        recorder.event(
            "INFO",
            "collect_options",
            "OPTIONS_COLLECTED",
            f"Collected {len(options)} options",
            option_count=len(options),
        )

        return options
    except Exception as e:
        recorder.event(
            "ERROR",
            "collect_options",
            "EXCEPTION",
            str(e),
        )
        return options


# =============================================================================
# SERVICE TYPE SELECTION WITH RETRIES
# =============================================================================


def _select_assist_option_with_retries(
    driver,
    container_id: str,
    target_value: str,
    target_label: str,
    recorder: DiagnosticsRecorder,
    max_retries: int = 3,
) -> bool:
    """
    Select Assist option with retry strategies:
    1. Full label → type, find by data-value, click
    2. Prefix → type prefix, find, click
    3. DOM search → open, find in DOM, click via JS
    """
    react_input = _find_assist_react_input(driver, container_id)
    hidden_input = None
    try:
        hidden_input = driver.find_element(By.ID, f"{container_id}_hidden")
    except Exception:
        pass

    strategies = [
        ("full_label", target_label, target_label),
        ("prefix", target_label.split()[0] if target_label else "", target_label),
        (
            "dom_search",
            "",
            target_label,
        ),  # DOM search doesn't need typing
    ]

    for strategy_name, query, expected in strategies:
        try:
            if not react_input:
                react_input = _find_assist_react_input(driver, container_id)
            if not react_input:
                continue

            # Clear input
            react_input.clear()
            time.sleep(0.2)

            # Strategy-specific action
            if strategy_name in ("full_label", "prefix"):
                # Type the query
                react_input.send_keys(query)
                time.sleep(0.5)

                # Find matching option
                option_elements = driver.find_elements(
                    By.XPATH, "//div[@role='option'] | //li[contains(@class,'option')]"
                )
                for elem in option_elements:
                    elem_text = _normalize_text(elem.text)
                    if elem_text.lower() == expected.lower():
                        elem.click()
                        time.sleep(0.3)
                        # Verify selection
                        if hidden_input:
                            if hidden_input.get_attribute("value") == target_value:
                                recorder.event(
                                    "INFO",
                                    "select_option",
                                    f"STRATEGY_{strategy_name.upper()}",
                                    f"Selected: {target_label}",
                                    context={"strategy": strategy_name},
                                )
                                return True
                        else:
                            # No hidden input to verify, assume success
                            return True

            elif strategy_name == "dom_search":
                # Open dropdown
                react_input.click()
                time.sleep(0.5)

                # Search in DOM for matching option
                option_elements = driver.find_elements(
                    By.XPATH,
                    "//div[@role='option'] | //li[contains(@class,'option')]",
                )
                for elem in option_elements:
                    elem_text = _normalize_text(elem.text)
                    if elem_text.lower() == expected.lower():
                        # Scroll into view
                        driver.execute_script("arguments[0].scrollIntoView(true);", elem)
                        time.sleep(0.2)
                        # Click via JS for reliability
                        driver.execute_script("arguments[0].click();", elem)
                        time.sleep(0.3)
                        # Verify
                        if hidden_input:
                            if hidden_input.get_attribute("value") == target_value:
                                recorder.event(
                                    "INFO",
                                    "select_option",
                                    "STRATEGY_DOM_SEARCH",
                                    f"Selected: {target_label}",
                                    context={"strategy": "dom_search"},
                                )
                                return True
                        else:
                            return True

        except Exception as e:
            recorder.event(
                "WARN",
                "select_option",
                f"STRATEGY_{strategy_name.upper()}_FAILED",
                str(e),
                context={"strategy": strategy_name},
            )
            continue

    # All strategies failed
    recorder.event(
        "ERROR",
        "select_option",
        "SELECT_FAILED_ALL_STRATEGIES",
        f"Could not select {target_label}",
    )
    return False


# =============================================================================
# VARIANT TABLE EXTRACTION
# =============================================================================


def _extract_variant_table_rows(driver, recorder: DiagnosticsRecorder) -> List[Dict]:
    """
    Extract variant rows from Assist appointment details table.
    Returns list of variant dicts with keys:
    - Service Variant Label
    - Rate
    - Code
    - Unit
    """
    rows = []

    # Multiple XPath selectors to find variant table
    selectors = [
        "//table[.//th[normalize-space(text())='Service']]",
        "//div[contains(@class,'table')][.//span[contains(text(),'Service')]]",
        "//div[@role='table'][.//div[@role='columnheader' and contains(text(),'Service')]]",
        "//table",  # Fallback to any table
    ]

    for selector in selectors:
        try:
            table = driver.find_element(By.XPATH, selector)

            # Extract rows
            row_elements = table.find_elements(By.XPATH, ".//tr[td or @role='row']")

            if not row_elements:
                continue

            for row_elem in row_elements:
                try:
                    # Get cells
                    cells = row_elem.find_elements(By.XPATH, ".//td | .//div[@role='cell']")

                    if len(cells) < 3:  # Need at least Service, Rate, Code
                        continue

                    # Extract cell values
                    variant_label = _normalize_text(cells[0].text)
                    rate_raw = _normalize_text(
                        cells[1].get_attribute("value") or cells[1].text
                    )
                    code_raw = _normalize_text(
                        cells[2].get_attribute("value") or cells[2].text
                    )
                    unit = (
                        _normalize_text(cells[3].text) if len(cells) > 3 else ""
                    )

                    if variant_label:  # Only include if we have a label
                        rate_norm = _coerce_rate_text(rate_raw)
                        code_norm = _normalize_token(code_raw)

                        rows.append(
                            {
                                "Service Variant Label": variant_label,
                                "Rate": rate_norm,
                                "Rate (Raw)": rate_raw,
                                "Code": code_norm,
                                "Code (Raw)": code_raw,
                                "Unit": unit,
                            }
                        )

                except Exception:
                    continue

            if rows:
                recorder.event(
                    "INFO",
                    "extract_variants",
                    CHECKER_VARIANT_TABLE_EXTRACT,
                    f"Extracted {len(rows)} variant rows",
                    option_count=len(rows),
                )
                return rows

        except Exception:
            continue

    # No table found
    if not rows:
        recorder.event(
            "WARN",
            "extract_variants",
            CHECKER_VARIANT_TABLE_MISSING,
            "Variant table not found",
        )

    return rows


# =============================================================================
# CONFLICT DETECTION
# =============================================================================


def _detect_conflicts(variants: List[Dict[str, str]]) -> Tuple[List[Dict], List[Dict]]:
    """
    Detect conflicting variants across probe clients.
    Returns (clean_rows, conflict_rows) where conflict rows have
    "Conflict" = "YES" and conflict detail.
    """
    by_key = {}
    for v in variants:
        key = (v["Parent Service Type ID"], v["Service Variant Label"])
        if key not in by_key:
            by_key[key] = []
        by_key[key].append(v)

    clean_rows = []
    conflict_rows = []

    for key, rows in by_key.items():
        rates = set(r["Rate"] for r in rows if r.get("Rate"))
        codes = set(r["Code"] for r in rows if r.get("Code"))

        if len(rates) > 1 or len(codes) > 1:
            for r in rows:
                r["Conflict"] = "YES"
                r["Conflict Detail"] = f"rates={rates} | codes={codes}"
            conflict_rows.extend(rows)
        else:
            for r in rows:
                r["Conflict"] = "NO"
                r["Conflict Detail"] = ""
            clean_rows.extend(rows)

    return clean_rows, conflict_rows


# =============================================================================
# MAIN EXTRACTION PIPELINE
# =============================================================================


def extract_service_type_variants(
    *,
    headless: bool = True,
    probe_client_ids: Union[str, List[str]],
    on_progress: ProgressCallback = None,
    on_event: EventCallback = None,
    on_row: RowCallback = None,
    resume: bool = True,
    force_refresh: bool = False,
) -> Dict[str, object]:
    """
    Extract all Service Type variants from Assist appointments form.

    Args:
        headless: Run browser headless
        probe_client_ids: Client ID(s) to probe (str or list)
        on_progress: Progress callback
        on_event: Event callback
        on_row: Row callback (called for each variant row)
        resume: Resume from checkpoint if exists
        force_refresh: Reprocess all even if checkpointed

    Returns:
        Summary dict with stats
    """
    # Normalize probe_client_ids
    if isinstance(probe_client_ids, str):
        probe_client_ids = [probe_client_ids]
    probe_client_ids = [str(cid).strip() for cid in probe_client_ids if cid]

    # Create run ID
    run_id = line_item_paths.make_run_id("VARIANTS")
    _emit(f"Run ID: {run_id}", on_progress)

    # Setup diagnostics
    paths = line_item_paths.get_variant_paths(run_id)
    diag_dir = paths["diagnostics_dir"]
    recorder = DiagnosticsRecorder(run_id, diag_dir)

    # Load checkpoint if resuming
    checkpoint = None
    if resume:
        checkpoint = _load_checkpoint(run_id)
        if checkpoint:
            _emit(f"Resumed from checkpoint: {len(checkpoint.get('processed_service_type_ids', []))} processed", on_progress)

    if not checkpoint:
        checkpoint = {
            "run_id": run_id,
            "started_at": _utc_now(),
            "probe_clients": probe_client_ids,
            "processed_service_type_ids": [],
            "failed_service_type_ids": [],
            "total_variant_rows": 0,
            "status": "in_progress",
        }

    processed_ids = set(checkpoint.get("processed_service_type_ids", []))
    failed_ids = set(checkpoint.get("failed_service_type_ids", []))
    all_variants = []

    driver = None
    try:
        # Ensure Chrome driver
        ensure_credentials()
        driver = build_chrome_driver(headless=headless)
        login(driver)

        # Navigate to Assist
        _emit("Opening Assist appointments form...", on_progress)
        if not _open_assist_appointments_new(driver, recorder):
            raise Exception("Failed to open Assist appointments form")

        # Collect all Service Types from all probe clients
        all_service_types = {}  # value -> label mapping
        for client_id in probe_client_ids:
            try:
                _emit(f"Collecting Service Types for client {client_id}...", on_progress)

                # Collect options
                container_id = "service_type_select"  # Assumed container ID
                options = _collect_assist_options(driver, container_id, recorder)

                for opt in options:
                    value = opt.get("value")
                    label = opt.get("label")
                    if value and label:
                        if value not in all_service_types:
                            all_service_types[value] = label

                _emit(f"Found {len(options)} Service Types for client {client_id}", on_progress)

            except Exception as e:
                recorder.event(
                    "ERROR",
                    "collect_service_types",
                    "EXCEPTION",
                    str(e),
                    client_id=client_id,
                )
                continue

        _emit(f"Total unique Service Types: {len(all_service_types)}", on_progress)

        # Extract variants for each Service Type
        for st_value, st_label in sorted(all_service_types.items()):
            if force_refresh:
                processed_flag = False
            else:
                processed_flag = st_value in processed_ids

            if processed_flag:
                _emit(f"Skipping {st_label} (already processed)", on_progress)
                continue

            try:
                _emit(f"Extracting variants for {st_label}...", on_progress)

                # Select Service Type
                container_id = "service_type_select"
                success = _select_assist_option_with_retries(
                    driver,
                    container_id,
                    st_value,
                    st_label,
                    recorder,
                )

                if not success:
                    failed_ids.add(st_value)
                    checkpoint["failed_service_type_ids"] = list(failed_ids)
                    _update_checkpoint(run_id, checkpoint)
                    continue

                # Extract variants from table
                time.sleep(1)  # Wait for table to render
                variants = _extract_variant_table_rows(driver, recorder)

                if not variants:
                    failed_ids.add(st_value)
                    checkpoint["failed_service_type_ids"] = list(failed_ids)
                    _update_checkpoint(run_id, checkpoint)
                    continue

                # Build full variant records
                variant_records = []
                for variant in variants:
                    record = {
                        "Parent Service Type ID": st_value,
                        "Parent Service Type Label": st_label,
                        "Service Variant Label": variant.get("Service Variant Label", ""),
                        "Rate": variant.get("Rate", ""),
                        "Rate (Raw)": variant.get("Rate (Raw)", ""),
                        "Code": variant.get("Code", ""),
                        "Code (Raw)": variant.get("Code (Raw)", ""),
                        "Unit": variant.get("Unit", ""),
                        "Conflict": "UNKNOWN",  # Will be determined after all clients
                        "Probe Client ID": probe_client_ids[0] if probe_client_ids else "",
                        "Source URL": driver.current_url,
                        "Captured At (UTC)": _utc_now(),
                    }
                    variant_records.append(record)
                    if on_row:
                        on_row(record)

                # Append to checkpoint CSV
                _append_to_checkpoint_csv(run_id, variant_records)
                all_variants.extend(variant_records)

                # Update checkpoint
                processed_ids.add(st_value)
                checkpoint["processed_service_type_ids"] = list(processed_ids)
                checkpoint["total_variant_rows"] = len(all_variants)
                _update_checkpoint(run_id, checkpoint)

                _emit(
                    f"  → extracted {len(variants)} variants",
                    on_progress,
                )

            except Exception as e:
                recorder.event(
                    "ERROR",
                    "extract_service_type",
                    "EXCEPTION",
                    str(e),
                    context={"service_type": st_label},
                )
                failed_ids.add(st_value)
                continue

        # Detect conflicts
        _emit("Detecting conflicts...", on_progress)
        clean_variants, conflict_variants = _detect_conflicts(all_variants)

        # Write outputs
        _emit("Writing outputs...", on_progress)

        # Latest CSV/XLSX
        _write_csv(all_variants, VARIANT_COLUMNS, paths["latest_csv"])
        _write_xlsx(all_variants, VARIANT_COLUMNS, paths["latest_xlsx"])
        _apply_xlsx_merged_cells(paths["latest_xlsx"])

        # Snapshot CSV/XLSX
        _write_csv(all_variants, VARIANT_COLUMNS, paths["snapshot_csv"])
        _write_xlsx(all_variants, VARIANT_COLUMNS, paths["snapshot_xlsx"])
        _apply_xlsx_merged_cells(paths["snapshot_xlsx"])

        # Conflicts CSV
        if conflict_variants:
            conflict_cols = VARIANT_COLUMNS + ["Conflict Detail"]
            _write_csv(conflict_variants, conflict_cols, paths["conflicts_csv"])

        # Finalize checkpoint
        checkpoint["status"] = "completed"
        checkpoint["completed_at"] = _utc_now()
        _update_checkpoint(run_id, checkpoint)

        # Save diagnostics
        recorder.save()

        summary = {
            "run_id": run_id,
            "total_service_types": len(all_service_types),
            "processed_service_types": len(processed_ids),
            "failed_service_types": len(failed_ids),
            "total_variant_rows": len(all_variants),
            "clean_variants": len(clean_variants),
            "conflict_variants": len(conflict_variants),
            "output_paths": {
                "latest_csv": str(paths["latest_csv"]),
                "latest_xlsx": str(paths["latest_xlsx"]),
                "snapshot_csv": str(paths["snapshot_csv"]),
                "snapshot_xlsx": str(paths["snapshot_xlsx"]),
                "conflicts_csv": str(paths["conflicts_csv"]),
                "diagnostics_dir": str(diag_dir),
            },
        }

        _emit(f"Extraction complete! Summary: {summary}", on_progress)

        return summary

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


# =============================================================================
# LEGACY FUNCTION: load_discovery_latest (for backward compatibility)
# =============================================================================


def load_discovery_latest(path: Path = None) -> List[Dict[str, str]]:
    """Load latest discovery data (legacy function)."""
    if path is None:
        paths = line_item_paths.get_variant_paths(
            line_item_paths.make_run_id("LEGACY")
        )
        path = paths["latest_csv"]

    if not path.exists():
        return []

    try:
        rows = []
        with open(path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        return rows
    except Exception:
        return []

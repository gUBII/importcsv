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
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from importcsv import (
    BASE_URL,
    build_chrome_driver,
    ensure_credentials,
    login,
    log_message,
)
import line_item_paths
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
    "Status",
    "Error Reason",
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

ASSIST_DIRECT_URL = "https://assist.turnpoint.co/appointments/new"

SERVICE_TYPE_LOCATOR_STRATEGIES: List[Tuple[str, str, str]] = [
    ("data-testid", By.CSS_SELECTOR, "[data-testid='service-type-combobox']"),
    ("name", By.CSS_SELECTOR, "input[name='service_type_id'][role='combobox']"),
    ("aria-label", By.CSS_SELECTOR, "input[aria-label='Service Type'][role='combobox']"),
    (
        "role-aria",
        By.CSS_SELECTOR,
        "input[role='combobox'][aria-haspopup='true']",
    ),
    (
        "label-adjacent-xpath",
        By.XPATH,
        "//label[contains(normalize-space(.), 'Service Type')]/following::input[@role='combobox'][1]",
    ),
]

SERVICE_TYPE_OPTION_LOCATOR_STRATEGIES: List[Tuple[str, str, str]] = [
    ("role-option", By.CSS_SELECTOR, "[role='option']"),
    ("react-option", By.CSS_SELECTOR, ".react-select__option"),
    ("list-item-option", By.XPATH, "//li[contains(@class, 'option')]"),
]

VARIANT_TABLE_LOCATOR_STRATEGIES: List[Tuple[str, str, str]] = [
    ("data-testid", By.CSS_SELECTOR, "[data-testid='service-variants-table']"),
    ("named-table", By.CSS_SELECTOR, "table[aria-label*='Service']"),
    (
        "table-with-service-header",
        By.XPATH,
        "//table[.//th[contains(normalize-space(.), 'Service')]]",
    ),
    (
        "first-table-after-service-type",
        By.XPATH,
        "//label[contains(normalize-space(.), 'Service Type')]/following::table[1]",
    ),
    ("fallback-any-table", By.CSS_SELECTOR, "table"),
]

MODAL_DISMISS_BUTTON_XPATH = (
    "//button[normalize-space()='Close' or normalize-space()='Dismiss' or normalize-space()='OK']"
    " | //button[contains(normalize-space(.), 'close') or contains(normalize-space(.), 'Dismiss')]"
    " | //div[@role='dialog']//button"
)

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
    text = _normalize_text(value)
    matches = re.findall(r"\d[\d,]*\.?\d*", text)
    if not matches:
        return text
    return matches[0].replace(",", "")


def _normalize_code_text(value: str) -> str:
    """Normalize item code while preserving underscore separators."""
    if not value:
        return ""
    cleaned = re.sub(r"[^A-Za-z0-9_]", "", _normalize_text(value))
    return cleaned.upper()


def _split_rate_and_unit(value: str, unit_hint: str = "") -> Tuple[str, str]:
    """Split a rate cell into normalized numeric rate and raw unit text."""
    raw = _normalize_text(value)
    rate = _coerce_rate_text(raw)
    unit = _normalize_text(unit_hint)
    if not unit and "/" in raw:
        unit = _normalize_text(raw[raw.index("/") :])
    return rate, unit


def _is_interactable(element) -> bool:
    """Best-effort interactable check that also works with test doubles."""
    if element is None:
        return False
    try:
        displayed = element.is_displayed()
    except Exception:
        displayed = True
    try:
        enabled = element.is_enabled()
    except Exception:
        enabled = True
    return bool(displayed and enabled)


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

    def __init__(
        self,
        run_id: str,
        output_dir: Path,
        on_event: EventCallback = None,
    ):
        self.run_id = run_id
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.events: List[Dict[str, str]] = []
        self.checkers: List[Dict[str, str]] = []
        self._event_callback = on_event

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
        if self._event_callback:
            try:
                self._event_callback(dict(event_dict))
            except Exception:
                pass

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
        with open(events_path, "w", encoding="utf-8") as f:
            for event in self.events:
                f.write(json.dumps(event) + "\n")

        # Save checkers as CSV
        checkers_path = self.output_dir / "checkers.csv"
        with open(checkers_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CHECKER_FIELDS)
            writer.writeheader()
            if self.checkers:
                writer.writerows(self.checkers)

    def capture_screenshot(self, driver, name: str) -> Optional[Path]:
        """Capture screenshot and save."""
        path = self.output_dir / f"{name}.png"
        try:
            driver.save_screenshot(str(path))
            return path
        except Exception:
            return None

    def capture_html(self, driver, name: str) -> Optional[Path]:
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

        # Merge contiguous parent groups in columns A and B.
        group_start = None
        current_parent = None
        for row_idx in range(2, ws.max_row + 2):
            parent_id = ws.cell(row_idx, 1).value if row_idx <= ws.max_row else None
            if parent_id != current_parent:
                if current_parent and group_start and row_idx - group_start > 1:
                    end_row = row_idx - 1
                    try:
                        ws.merge_cells(f"A{group_start}:A{end_row}")
                        ws.merge_cells(f"B{group_start}:B{end_row}")
                    except Exception:
                        pass
                    ws.cell(group_start, 1).alignment = Alignment(vertical="center")
                    ws.cell(group_start, 2).alignment = Alignment(vertical="center")
                current_parent = parent_id
                group_start = row_idx if parent_id else None

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


def _switch_to_default_content(driver) -> None:
    """Switch to top-level document when Selenium context supports it."""
    try:
        if hasattr(driver, "switch_to"):
            driver.switch_to.default_content()
    except Exception:
        pass


def _switch_to_frame(driver, frame_elem) -> bool:
    """Switch into a frame if possible."""
    try:
        if hasattr(driver, "switch_to"):
            driver.switch_to.frame(frame_elem)
            return True
    except Exception:
        return False
    return False


def _record_locator_success(
    recorder: DiagnosticsRecorder,
    *,
    step: str,
    code: str,
    strategy: str,
    selector: str,
    frame_hint: str,
) -> None:
    recorder.event(
        "INFO",
        step,
        code,
        f"Locator strategy '{strategy}' matched in {frame_hint}",
        selector=selector,
        context={"strategy": strategy, "frame": frame_hint},
    )


def _capture_assist_failure_artifacts(
    driver,
    recorder: DiagnosticsRecorder,
    *,
    prefix: str,
    reason: str,
) -> Tuple[Optional[Path], Optional[Path]]:
    """Capture screenshot + HTML + console logs for failure forensics."""
    safe_prefix = _normalize_token(prefix) or "assist"
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    screenshot = recorder.capture_screenshot(driver, f"{safe_prefix}_{ts}")
    html = recorder.capture_html(driver, f"{safe_prefix}_{ts}")

    current_url = _normalize_text(getattr(driver, "current_url", ""))
    title = _normalize_text(getattr(driver, "title", ""))
    body_snippet_len = 0
    try:
        body = _normalize_text(driver.find_element(By.TAG_NAME, "body").text)
        body_snippet_len = len(body[:500])
    except Exception:
        pass

    console_path = recorder.output_dir / f"{safe_prefix}_{ts}_console.json"
    console_saved = False
    try:
        logs = driver.get_log("browser")
        with open(console_path, "w", encoding="utf-8") as fh:
            json.dump(logs, fh, indent=2, default=str)
        console_saved = True
    except Exception:
        console_saved = False

    recorder.event(
        "ERROR",
        "assist_artifacts",
        "ASSIST_FAILURE_ARTIFACTS",
        reason,
        url=current_url,
        context={
            "current_url": current_url,
            "title": title,
            "body_snippet_len": body_snippet_len,
            "html_artifact": html.name if html else "",
            "screenshot_artifact": screenshot.name if screenshot else "",
            "console_artifact": console_path.name if console_saved else "",
        },
    )
    return screenshot, html


def _dismiss_blocking_modal_if_present(driver, recorder: DiagnosticsRecorder) -> bool:
    """Dismiss an obvious blocking modal when present."""
    dismissed = False
    for strategy_name, by, selector in [
        ("modal-close-buttons", By.XPATH, MODAL_DISMISS_BUTTON_XPATH),
        ("modal-role-dialog", By.CSS_SELECTOR, "[role='dialog'] button"),
        ("bootstrap-modal", By.CSS_SELECTOR, ".modal button"),
    ]:
        try:
            buttons = driver.find_elements(by, selector)
        except Exception:
            buttons = []
        for button in buttons:
            if not _is_interactable(button):
                continue
            try:
                button.click()
                dismissed = True
                recorder.event(
                    "INFO",
                    "dismiss_modal",
                    "MODAL_DISMISSED",
                    f"Dismissed blocking modal using {strategy_name}",
                    selector=selector,
                    context={"strategy": strategy_name},
                )
                time.sleep(0.2)
                return True
            except Exception:
                continue
    return dismissed


def _locate_service_type_combobox(
    driver,
    recorder: DiagnosticsRecorder,
    *,
    timeout: int = 12,
    require_interactable: bool = True,
) -> Tuple[Optional[Any], str]:
    """
    Locate Service Type combobox in main document or inside iframes.
    Returns (element, frame_hint). On success, leaves driver context where found.
    """
    deadline = time.time() + timeout
    attempts = []

    while time.time() < deadline:
        _switch_to_default_content(driver)

        # Search main document first.
        for strategy_name, by, selector in SERVICE_TYPE_LOCATOR_STRATEGIES:
            attempts.append(strategy_name)
            try:
                elements = driver.find_elements(by, selector)
            except Exception:
                elements = []
            for element in elements:
                if not require_interactable or _is_interactable(element):
                    _record_locator_success(
                        recorder,
                        step="locate_service_type",
                        code="SERVICE_TYPE_LOCATOR_HIT",
                        strategy=strategy_name,
                        selector=selector,
                        frame_hint="main",
                    )
                    return element, "main"

        # Search each iframe.
        try:
            frames = driver.find_elements(By.TAG_NAME, "iframe")
        except Exception:
            frames = []
        for idx, frame in enumerate(frames):
            _switch_to_default_content(driver)
            if not _switch_to_frame(driver, frame):
                continue
            frame_hint = f"iframe[{idx}]"
            for strategy_name, by, selector in SERVICE_TYPE_LOCATOR_STRATEGIES:
                attempts.append(f"{strategy_name}:{frame_hint}")
                try:
                    elements = driver.find_elements(by, selector)
                except Exception:
                    elements = []
                for element in elements:
                    if not require_interactable or _is_interactable(element):
                        _record_locator_success(
                            recorder,
                            step="locate_service_type",
                            code="SERVICE_TYPE_LOCATOR_HIT",
                            strategy=strategy_name,
                            selector=selector,
                            frame_hint=frame_hint,
                        )
                        return element, frame_hint

        _dismiss_blocking_modal_if_present(driver, recorder)
        time.sleep(0.35)

    _switch_to_default_content(driver)
    recorder.event(
        "WARN",
        "locate_service_type",
        "SERVICE_TYPE_LOCATOR_MISS",
        "Unable to locate Service Type combobox",
        context={"attempts": attempts[-20:]},
    )
    return None, "main"


def _find_service_type_reload_button(driver) -> Optional[Any]:
    """Locate optional service-type reload control near the combobox."""
    strategies = [
        (By.CSS_SELECTOR, "[data-cy='service_type_id-reload']"),
        (By.XPATH, "//div[@role='button' and @data-cy='service_type_id-reload']"),
        (By.XPATH, "//label[contains(normalize-space(.), 'Service Type')]/following::*[@role='button'][1]"),
    ]
    for by, selector in strategies:
        try:
            buttons = driver.find_elements(by, selector)
        except Exception:
            buttons = []
        for button in buttons:
            if _is_interactable(button):
                return button
    return None


def _wait_for_service_type_options(driver, timeout: float = 4.0) -> List[Any]:
    """Wait for Service Type option elements to appear."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for _, by, selector in SERVICE_TYPE_OPTION_LOCATOR_STRATEGIES:
            try:
                options = driver.find_elements(by, selector)
            except Exception:
                options = []
            visible = [opt for opt in options if _normalize_text(getattr(opt, "text", ""))]
            if visible:
                return visible
        time.sleep(0.2)
    return []


def _click_matching_option(driver, target_label: str, target_value: str = "") -> bool:
    """Click best-matching option in open combobox list."""
    options = _wait_for_service_type_options(driver, timeout=5.0)
    if not options:
        return False

    norm_target_label = _normalize_text(target_label).lower()
    norm_target_value = _normalize_text(target_value).lower()

    # 1) exact label
    for option in options:
        label = _normalize_text(getattr(option, "text", ""))
        if label.lower() == norm_target_label and label:
            option.click()
            return True

    # 2) data-value / value match
    for option in options:
        value = _normalize_text(option.get_attribute("data-value") or option.get_attribute("value"))
        if value and norm_target_value and value.lower() == norm_target_value:
            option.click()
            return True

    # 3) prefix label
    for option in options:
        label = _normalize_text(getattr(option, "text", ""))
        if norm_target_label and label.lower().startswith(norm_target_label[:18]):
            try:
                option.click()
            except Exception:
                driver.execute_script("arguments[0].click();", option)
            return True

    return False


def _wait_for_assist_ready(driver, recorder: DiagnosticsRecorder, timeout: int = 20) -> bool:
    """Readiness gate: Service Type combobox must be present and interactable."""
    element, frame_hint = _locate_service_type_combobox(
        driver,
        recorder,
        timeout=timeout,
        require_interactable=True,
    )
    if not element:
        return False
    if not _is_interactable(element):
        return False
    recorder.checker(
        "assist_readiness",
        "PASS",
        CHECKER_SERVICE_TYPE_SELECT,
        "Service Type combobox located and interactable",
        url=_normalize_text(getattr(driver, "current_url", "")),
        selector="; ".join(s for _, _, s in SERVICE_TYPE_LOCATOR_STRATEGIES[:3]),
    )
    recorder.event(
        "INFO",
        "assist_readiness",
        "ASSIST_READY",
        f"Assist readiness passed in {frame_hint}",
        context={"frame": frame_hint},
    )
    return True


def _open_assist_appointments_new(
    driver,
    recorder: DiagnosticsRecorder,
    probe_client_id: str = "",
) -> bool:
    """
    Open appointment editor using bridge and direct routes.
    Success criteria: Service Type combobox is interactable.
    """
    attempts: List[Tuple[str, str]] = []
    bridge_url = (
        f"{BASE_URL.rstrip('/')}/appointment-edit.asp?NavShow=none&createRepeat=no&cid={probe_client_id}&appointmentDate_override="
        if probe_client_id
        else ""
    )
    route_candidates: List[Tuple[str, str]] = []
    if bridge_url:
        route_candidates.append(("tp1_bridge_direct", bridge_url))
    route_candidates.append(("assist_direct", ASSIST_DIRECT_URL))
    if probe_client_id:
        route_candidates.append(
            (
                "tp1_client_appointments",
                f"{BASE_URL.rstrip('/')}/client-details.asp?eid={probe_client_id}&BREAKDOWN_SHOW_APPOINTMENTS=yes",
            )
        )

    for route_name, url in route_candidates:
        attempts.append((route_name, url))
        try:
            driver.get(url)
            WebDriverWait(driver, 25).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            time.sleep(0.4)

            if route_name == "tp1_client_appointments":
                add_selectors = [
                    (By.XPATH, "//a[contains(normalize-space(.), 'Add Appointment')]"),
                    (By.XPATH, "//button[contains(normalize-space(.), 'Add Appointment')]"),
                    (By.XPATH, "//a[contains(@href, 'appointment-edit.asp')]"),
                ]
                clicked = False
                for by, selector in add_selectors:
                    try:
                        elems = driver.find_elements(by, selector)
                    except Exception:
                        elems = []
                    for elem in elems:
                        if _is_interactable(elem):
                            try:
                                elem.click()
                            except Exception:
                                driver.execute_script("arguments[0].click();", elem)
                            clicked = True
                            time.sleep(0.8)
                            break
                    if clicked:
                        break
                if not clicked and bridge_url:
                    driver.get(bridge_url)
                    time.sleep(0.8)

            if _wait_for_assist_ready(driver, recorder, timeout=12):
                recorder.checker(
                    "open_assist",
                    "PASS",
                    CHECKER_APPOINTMENT,
                    f"Assist opened via {route_name}",
                    url=_normalize_text(getattr(driver, "current_url", "")),
                )
                return True

            _capture_assist_failure_artifacts(
                driver,
                recorder,
                prefix=f"assist_open_fail_{route_name}",
                reason=f"Readiness failed after opening route {route_name}",
            )
        except Exception as exc:
            screenshot, html = _capture_assist_failure_artifacts(
                driver,
                recorder,
                prefix=f"assist_open_exception_{route_name}",
                reason=f"Route {route_name} failed: {exc}",
            )
            recorder.checker(
                "open_assist",
                "FAIL",
                CHECKER_APPOINTMENT,
                f"Route {route_name} exception: {exc}",
                url=_normalize_text(getattr(driver, "current_url", "")),
                screenshot=screenshot,
                html=html,
            )

    recorder.checker(
        "open_assist",
        "FAIL",
        CHECKER_APPOINTMENT,
        f"All assist entry routes failed: {attempts}",
        url=_normalize_text(getattr(driver, "current_url", "")),
    )
    return False


def _collect_assist_options(driver, recorder: DiagnosticsRecorder) -> List[Dict[str, str]]:
    """Extract visible Service Type options from combobox list."""
    options: List[Dict[str, str]] = []
    combobox, _ = _locate_service_type_combobox(
        driver,
        recorder,
        timeout=10,
        require_interactable=True,
    )
    if not combobox:
        return options

    try:
        combobox.click()
    except Exception:
        driver.execute_script("arguments[0].click();", combobox)
    time.sleep(0.3)

    option_elements = _wait_for_service_type_options(driver, timeout=5.0)
    for elem in option_elements:
        label = _normalize_text(getattr(elem, "text", ""))
        value = _normalize_text(elem.get_attribute("data-value") or elem.get_attribute("value"))
        if label:
            options.append({"label": label, "value": value or label})

    # Dismiss open list.
    try:
        combobox.send_keys(Keys.ESCAPE)
    except Exception:
        pass

    recorder.event(
        "INFO",
        "collect_options",
        "OPTIONS_COLLECTED",
        f"Collected {len(options)} options from Assist",
        option_count=len(options),
    )
    return options


# =============================================================================
# SERVICE TYPE SELECTION WITH RETRIES
# =============================================================================


def _select_assist_option_with_retries(
    driver,
    target_value: str,
    target_label: str,
    recorder: DiagnosticsRecorder,
    max_retries: int = 3,
) -> bool:
    """
    Select Service Type with layered fallbacks:
    1) click combobox + click exact option
    2) reload control + click option
    3) type label + Enter
    """
    strategies = ["click_option", "reload_then_click", "type_then_enter"]
    for attempt in range(max_retries):
        for strategy in strategies:
            try:
                combobox, frame_hint = _locate_service_type_combobox(
                    driver,
                    recorder,
                    timeout=6,
                    require_interactable=True,
                )
                if not combobox:
                    continue

                try:
                    combobox.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", combobox)
                time.sleep(0.2)

                if strategy == "reload_then_click":
                    reload_btn = _find_service_type_reload_button(driver)
                    if reload_btn:
                        try:
                            reload_btn.click()
                        except Exception:
                            driver.execute_script("arguments[0].click();", reload_btn)
                        time.sleep(0.4)
                        try:
                            combobox.click()
                        except Exception:
                            driver.execute_script("arguments[0].click();", combobox)
                        time.sleep(0.2)

                if strategy in {"click_option", "reload_then_click"}:
                    if _click_matching_option(driver, target_label, target_value):
                        recorder.event(
                            "INFO",
                            "select_option",
                            "SERVICE_TYPE_SELECTED",
                            f"Selected '{target_label}' using {strategy}",
                            context={"strategy": strategy, "frame": frame_hint},
                        )
                        return True

                if strategy == "type_then_enter":
                    try:
                        combobox.clear()
                    except Exception:
                        pass
                    combobox.send_keys(target_label)
                    time.sleep(0.35)
                    combobox.send_keys(Keys.ENTER)
                    recorder.event(
                        "INFO",
                        "select_option",
                        "SERVICE_TYPE_SELECTED",
                        f"Selected '{target_label}' using type+enter",
                        context={"strategy": strategy, "frame": frame_hint},
                    )
                    return True

            except Exception as exc:
                recorder.event(
                    "WARN",
                    "select_option",
                    "SELECT_STRATEGY_FAILED",
                    str(exc),
                    context={
                        "strategy": strategy,
                        "attempt": attempt + 1,
                        "target_label": target_label,
                    },
                )
                continue

    recorder.event(
        "ERROR",
        "select_option",
        "SELECT_FAILED_ALL_STRATEGIES",
        f"Could not select '{target_label}'",
        context={"target_label": target_label, "target_value": target_value},
    )
    return False


# =============================================================================
# VARIANT TABLE EXTRACTION
# =============================================================================


def _table_headers(table_elem) -> List[str]:
    """Extract normalized table headers."""
    headers = []
    try:
        header_cells = table_elem.find_elements(By.XPATH, ".//thead//th | .//tr[1]/th")
    except Exception:
        header_cells = []
    for cell in header_cells:
        token = _normalize_text(getattr(cell, "text", ""))
        if token:
            headers.append(token.lower())
    return headers


def _locate_variant_table(driver, recorder: DiagnosticsRecorder) -> Tuple[Optional[Any], str]:
    """Locate variant table with layered selectors."""
    for strategy_name, by, selector in VARIANT_TABLE_LOCATOR_STRATEGIES:
        try:
            tables = driver.find_elements(by, selector)
        except Exception:
            tables = []
        for table in tables:
            headers = _table_headers(table)
            if headers:
                joined = " ".join(headers)
                if "service" not in joined or "rate" not in joined or "code" not in joined:
                    if strategy_name != "fallback-any-table":
                        continue
            recorder.event(
                "INFO",
                "locate_variant_table",
                "VARIANT_TABLE_LOCATOR_HIT",
                f"Variant table found via {strategy_name}",
                selector=selector,
                context={"strategy": strategy_name, "headers": headers},
            )
            return table, strategy_name
    recorder.event(
        "WARN",
        "locate_variant_table",
        CHECKER_VARIANT_TABLE_MISSING,
        "Variant table not found via configured locators",
    )
    return None, ""


def _extract_cell_text(cell) -> str:
    """Extract text/value from cell including input-backed values."""
    value = _normalize_text(cell.get_attribute("value"))
    if value:
        return value
    try:
        inputs = cell.find_elements(By.XPATH, ".//input | .//textarea")
    except Exception:
        inputs = []
    for input_elem in inputs:
        value = _normalize_text(input_elem.get_attribute("value"))
        if value:
            return value
    return _normalize_text(getattr(cell, "text", ""))


def _wait_for_variants_to_stabilize(
    driver,
    recorder: DiagnosticsRecorder,
    timeout: float = 8.0,
) -> Tuple[Optional[Any], int]:
    """Wait until variant row count is stable for at least two polls."""
    deadline = time.time() + timeout
    prev_count = -1
    stable_cycles = 0
    last_table = None
    while time.time() < deadline:
        table, _ = _locate_variant_table(driver, recorder)
        if table is None:
            time.sleep(0.25)
            continue
        last_table = table
        try:
            row_elements = table.find_elements(By.CSS_SELECTOR, "tbody tr")
            if not row_elements:
                row_elements = table.find_elements(By.XPATH, ".//tr[td]")
        except Exception:
            row_elements = []
        count = len(row_elements)
        if count > 0 and count == prev_count:
            stable_cycles += 1
            if stable_cycles >= 2:
                return table, count
        else:
            stable_cycles = 0
        prev_count = count
        time.sleep(0.25)
    return last_table, max(prev_count, 0)


def _extract_variant_table_rows(driver, recorder: DiagnosticsRecorder) -> List[Dict]:
    """
    Extract variant rows from Assist appointment details table.
    Returns list of variant dicts with keys:
    - Service Variant Label
    - Rate
    - Rate (Raw)
    - Code
    - Code (Raw)
    - Unit
    """
    extracted_rows: List[Dict[str, str]] = []
    table, observed_count = _wait_for_variants_to_stabilize(driver, recorder, timeout=9.0)
    if table is None:
        recorder.checker(
            "extract_variants",
            "FAIL",
            CHECKER_VARIANT_TABLE_MISSING,
            "Variant table not found",
            url=_normalize_text(getattr(driver, "current_url", "")),
        )
        return extracted_rows

    try:
        row_elements = table.find_elements(By.CSS_SELECTOR, "tbody tr")
        if not row_elements:
            row_elements = table.find_elements(By.XPATH, ".//tr[td]")
    except Exception:
        row_elements = []

    for row_elem in row_elements:
        try:
            cells = row_elem.find_elements(By.CSS_SELECTOR, "td")
            if not cells:
                cells = row_elem.find_elements(By.XPATH, ".//td | .//div[@role='cell']")
            if len(cells) < 3:
                continue

            service_label = _extract_cell_text(cells[0])
            rate_raw = _extract_cell_text(cells[1])
            code_raw = _extract_cell_text(cells[2])
            unit_hint = _extract_cell_text(cells[3]) if len(cells) > 3 else ""
            rate_norm, unit = _split_rate_and_unit(rate_raw, unit_hint)
            code_norm = _normalize_code_text(code_raw)

            if not service_label:
                continue

            extracted_rows.append(
                {
                    "Service Variant Label": service_label,
                    "Rate": rate_norm,
                    "Rate (Raw)": rate_raw,
                    "Code": code_norm,
                    "Code (Raw)": code_raw,
                    "Unit": unit,
                }
            )
        except Exception:
            continue

    if extracted_rows:
        recorder.checker(
            "extract_variants",
            "PASS",
            CHECKER_VARIANT_TABLE_EXTRACT,
            f"Extracted {len(extracted_rows)} rows",
            option_count=len(extracted_rows),
            url=_normalize_text(getattr(driver, "current_url", "")),
        )
    else:
        code = CHECKER_VARIANT_TABLE_EMPTY if observed_count > 0 else CHECKER_VARIANT_TABLE_MISSING
        recorder.checker(
            "extract_variants",
            "FAIL",
            code,
            f"Variant table had no extractable rows (observed_row_count={observed_count})",
            option_count=observed_count,
            url=_normalize_text(getattr(driver, "current_url", "")),
        )
    return extracted_rows


def _load_service_types_from_reference_index(
    recorder: DiagnosticsRecorder,
) -> Dict[str, str]:
    """Load Service Type ID -> label mapping from latest reference export."""
    service_types: Dict[str, str] = {}
    candidates = [
        line_item_paths.reference_latest_csv(),
        line_item_paths.reference_latest_xlsx(),
    ]

    for source_path in candidates:
        if not source_path.exists():
            continue
        try:
            rows: List[Dict[str, str]] = []
            if source_path.suffix.lower() == ".csv":
                with open(source_path, "r", newline="", encoding="utf-8") as fh:
                    rows = list(csv.DictReader(fh))
            else:
                wb = load_workbook(source_path, read_only=True, data_only=True)
                ws = wb.active
                header = []
                for cell in ws[1]:
                    header.append(_normalize_text(cell.value))
                for values in ws.iter_rows(min_row=2, values_only=True):
                    row = {}
                    for idx, value in enumerate(values):
                        if idx < len(header) and header[idx]:
                            row[header[idx]] = _normalize_text(value)
                    rows.append(row)

            if not rows:
                continue

            for row in rows:
                id_value = (
                    row.get("ID")
                    or row.get("Service Type ID")
                    or row.get("ServiceTypeID")
                    or row.get("ServiceType Id")
                    or ""
                )
                label_value = (
                    row.get("Service Type")
                    or row.get("Name")
                    or row.get("ServiceType")
                    or row.get("Service Type Label")
                    or ""
                )
                id_value = _normalize_text(id_value)
                label_value = _normalize_text(label_value)
                if id_value and label_value and id_value not in service_types:
                    service_types[id_value] = label_value

            if service_types:
                recorder.event(
                    "INFO",
                    "service_type_index",
                    "REFERENCE_INDEX_LOADED",
                    f"Loaded {len(service_types)} Service Types from {source_path.name}",
                    option_count=len(service_types),
                    context={"path": str(source_path)},
                )
                return service_types
        except Exception as exc:
            recorder.event(
                "WARN",
                "service_type_index",
                "REFERENCE_INDEX_LOAD_FAILED",
                f"Could not load {source_path}: {exc}",
                context={"path": str(source_path)},
            )
            continue

    recorder.event(
        "WARN",
        "service_type_index",
        "REFERENCE_INDEX_MISSING",
        "No reference Service Type export found; falling back to UI options",
    )
    return service_types


def _build_variant_record(
    *,
    service_type_id: str,
    service_type_label: str,
    variant: Dict[str, str],
    probe_client_id: str,
    source_url: str,
) -> Dict[str, str]:
    return {
        "Parent Service Type ID": service_type_id,
        "Parent Service Type Label": service_type_label,
        "Service Variant Label": variant.get("Service Variant Label", ""),
        "Rate": variant.get("Rate", ""),
        "Rate (Raw)": variant.get("Rate (Raw)", ""),
        "Code": variant.get("Code", ""),
        "Code (Raw)": variant.get("Code (Raw)", ""),
        "Unit": variant.get("Unit", ""),
        "Status": "PASS",
        "Error Reason": "",
        "Conflict": "UNKNOWN",
        "Probe Client ID": probe_client_id,
        "Source URL": source_url,
        "Captured At (UTC)": _utc_now(),
    }


def _build_failure_record(
    *,
    service_type_id: str,
    service_type_label: str,
    probe_client_id: str,
    source_url: str,
    reason: str,
) -> Dict[str, str]:
    return {
        "Parent Service Type ID": service_type_id,
        "Parent Service Type Label": service_type_label,
        "Service Variant Label": "",
        "Rate": "",
        "Rate (Raw)": "",
        "Code": "",
        "Code (Raw)": "",
        "Unit": "",
        "Status": "FAIL",
        "Error Reason": _normalize_text(reason),
        "Conflict": "N/A",
        "Probe Client ID": probe_client_id,
        "Source URL": source_url,
        "Captured At (UTC)": _utc_now(),
    }


# =============================================================================
# CONFLICT DETECTION
# =============================================================================


def _detect_conflicts(variants: List[Dict[str, str]]) -> Tuple[List[Dict], List[Dict]]:
    """
    Detect conflicting variants across probe clients.
    Returns (clean_rows, conflict_rows) where conflict rows have
    "Conflict" = "YES" and conflict detail.
    """
    by_key: Dict[Tuple[str, str], List[Dict[str, str]]] = {}
    clean_rows: List[Dict[str, str]] = []
    conflict_rows: List[Dict[str, str]] = []

    for v in variants:
        if _normalize_text(v.get("Status", "PASS")).upper() == "FAIL":
            v["Conflict"] = "N/A"
            v["Conflict Detail"] = ""
            clean_rows.append(v)
            continue
        key = (v.get("Parent Service Type ID", ""), v.get("Service Variant Label", ""))
        if key not in by_key:
            by_key[key] = []
        by_key[key].append(v)

    for key, rows in by_key.items():
        rates = set(r.get("Rate", "") for r in rows if r.get("Rate"))
        codes = set(r.get("Code", "") for r in rows if r.get("Code"))

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
    smoke_mode: bool = False,
    smoke_service_type_id: str = "",
    smoke_service_type_label: str = "",
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
        smoke_mode: Restrict extraction to a single Service Type
        smoke_service_type_id: Optional Service Type ID to use in smoke mode
        smoke_service_type_label: Optional Service Type label to use in smoke mode

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
    recorder = DiagnosticsRecorder(run_id, diag_dir, on_event=on_event)

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
    all_variants: List[Dict[str, str]] = []
    all_service_types: Dict[str, str] = {}

    driver = None
    fatal_error = ""
    try:
        # Ensure Chrome driver
        ensure_credentials()
        line_item_paths.ensure_structure()
        download_dir = line_item_paths.downloads_dir()
        driver = build_chrome_driver(headless=headless, download_dir=download_dir)
        login(driver)

        # Navigate to Assist
        _emit("Opening Assist appointments form...", on_progress)
        preferred_probe = probe_client_ids[0] if probe_client_ids else ""
        if not _open_assist_appointments_new(driver, recorder, preferred_probe):
            raise RuntimeError("Failed to open Assist appointment editor with interactable Service Type combobox.")

        # Collect Service Types from exported index first, fallback to UI options.
        all_service_types = _load_service_types_from_reference_index(recorder)
        if not all_service_types:
            options = _collect_assist_options(driver, recorder)
            for opt in options:
                value = _normalize_text(opt.get("value") or "")
                label = _normalize_text(opt.get("label") or "")
                if value and label and value not in all_service_types:
                    all_service_types[value] = label

        if not all_service_types:
            raise RuntimeError("No Service Types available from reference index or Assist combobox options.")

        if smoke_mode:
            selected: Dict[str, str] = {}
            target_id = _normalize_text(smoke_service_type_id)
            target_label = _normalize_text(smoke_service_type_label).lower()
            if target_id and target_id in all_service_types:
                selected[target_id] = all_service_types[target_id]
            elif target_label:
                for st_id, st_label in sorted(all_service_types.items()):
                    if target_label in st_label.lower():
                        selected[st_id] = st_label
                        break
            if not selected:
                first_id = next(iter(sorted(all_service_types.keys())))
                selected[first_id] = all_service_types[first_id]
            all_service_types = selected
            recorder.event(
                "INFO",
                "smoke_mode",
                "SMOKE_MODE_ACTIVE",
                f"Smoke mode active for {len(all_service_types)} Service Type(s)",
                context=all_service_types,
            )

        _emit(f"Total Service Types queued: {len(all_service_types)}", on_progress)

        # Extract variants for each Service Type
        for st_value, st_label in sorted(all_service_types.items(), key=lambda kv: kv[1].lower()):
            if force_refresh:
                processed_flag = False
            else:
                processed_flag = st_value in processed_ids

            if processed_flag:
                _emit(f"Skipping {st_label} (already processed)", on_progress)
                continue

            source_url = _normalize_text(getattr(driver, "current_url", ""))
            try:
                _emit(f"Extracting variants for {st_label}...", on_progress)

                # Select Service Type
                success = _select_assist_option_with_retries(
                    driver,
                    st_value,
                    st_label,
                    recorder,
                )

                if not success:
                    reason = "Service Type selection failed after fallback strategies."
                    screenshot, html = _capture_assist_failure_artifacts(
                        driver,
                        recorder,
                        prefix=f"service_type_select_fail_{st_value}",
                        reason=reason,
                    )
                    recorder.checker(
                        "service_type_select",
                        "FAIL",
                        CHECKER_SERVICE_TYPE_SELECT,
                        reason,
                        url=_normalize_text(getattr(driver, "current_url", "")),
                        selector="Service Type combobox",
                        screenshot=screenshot,
                        html=html,
                    )

                    failure_row = _build_failure_record(
                        service_type_id=st_value,
                        service_type_label=st_label,
                        probe_client_id=preferred_probe,
                        source_url=_normalize_text(getattr(driver, "current_url", "")) or source_url,
                        reason=reason,
                    )
                    all_variants.append(failure_row)
                    _append_to_checkpoint_csv(run_id, [failure_row])
                    if on_row:
                        on_row(failure_row)

                    failed_ids.add(st_value)
                    checkpoint["failed_service_type_ids"] = list(failed_ids)
                    checkpoint["total_variant_rows"] = len(all_variants)
                    _update_checkpoint(run_id, checkpoint)
                    continue

                # Extract variants from table
                variants = _extract_variant_table_rows(driver, recorder)

                if not variants:
                    reason = "Variants table missing or empty for selected Service Type."
                    screenshot, html = _capture_assist_failure_artifacts(
                        driver,
                        recorder,
                        prefix=f"service_type_variants_missing_{st_value}",
                        reason=reason,
                    )
                    recorder.checker(
                        "extract_variants",
                        "FAIL",
                        CHECKER_VARIANT_TABLE_MISSING,
                        reason,
                        url=_normalize_text(getattr(driver, "current_url", "")),
                        screenshot=screenshot,
                        html=html,
                    )

                    failure_row = _build_failure_record(
                        service_type_id=st_value,
                        service_type_label=st_label,
                        probe_client_id=preferred_probe,
                        source_url=_normalize_text(getattr(driver, "current_url", "")) or source_url,
                        reason=reason,
                    )
                    all_variants.append(failure_row)
                    _append_to_checkpoint_csv(run_id, [failure_row])
                    if on_row:
                        on_row(failure_row)

                    failed_ids.add(st_value)
                    checkpoint["failed_service_type_ids"] = list(failed_ids)
                    checkpoint["total_variant_rows"] = len(all_variants)
                    _update_checkpoint(run_id, checkpoint)
                    continue

                # Build full variant records
                variant_records: List[Dict[str, str]] = []
                for variant in variants:
                    record = _build_variant_record(
                        service_type_id=st_value,
                        service_type_label=st_label,
                        variant=variant,
                        probe_client_id=preferred_probe,
                        source_url=_normalize_text(getattr(driver, "current_url", "")),
                    )
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
                reason = f"Exception during Service Type extraction: {e}"
                screenshot, html = _capture_assist_failure_artifacts(
                    driver,
                    recorder,
                    prefix=f"service_type_exception_{st_value}",
                    reason=reason,
                )
                recorder.event(
                    "ERROR",
                    "extract_service_type",
                    "EXCEPTION",
                    reason,
                    context={"service_type": st_label},
                )
                recorder.checker(
                    "extract_service_type",
                    "FAIL",
                    "CHK_SERVICE_TYPE_EXCEPTION",
                    reason,
                    url=_normalize_text(getattr(driver, "current_url", "")),
                    screenshot=screenshot,
                    html=html,
                )
                failure_row = _build_failure_record(
                    service_type_id=st_value,
                    service_type_label=st_label,
                    probe_client_id=preferred_probe,
                    source_url=_normalize_text(getattr(driver, "current_url", "")) or source_url,
                    reason=reason,
                )
                all_variants.append(failure_row)
                _append_to_checkpoint_csv(run_id, [failure_row])
                if on_row:
                    on_row(failure_row)
                failed_ids.add(st_value)
                checkpoint["failed_service_type_ids"] = list(failed_ids)
                checkpoint["total_variant_rows"] = len(all_variants)
                _update_checkpoint(run_id, checkpoint)
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
        checkpoint["processed_service_type_ids"] = list(processed_ids)
        checkpoint["failed_service_type_ids"] = list(failed_ids)
        checkpoint["total_variant_rows"] = len(all_variants)
        _update_checkpoint(run_id, checkpoint)

        summary = {
            "run_id": run_id,
            "total_service_types": len(all_service_types),
            "processed_service_types": len(processed_ids),
            "failed_service_types": len(failed_ids),
            "total_variant_rows": len(all_variants),
            "clean_variants": len(clean_variants),
            "conflict_variants": len(conflict_variants),
            "smoke_mode": smoke_mode,
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
    except Exception as exc:
        fatal_error = str(exc)
        recorder.event(
            "ERROR",
            "extract_run",
            "RUN_FAILED",
            fatal_error,
            context={"traceback": traceback.format_exc()},
        )
        if driver:
            screenshot, html = _capture_assist_failure_artifacts(
                driver,
                recorder,
                prefix="run_failure",
                reason=fatal_error,
            )
            recorder.checker(
                "extract_run",
                "FAIL",
                "CHK_VARIANT_EXTRACTION_RUN",
                fatal_error,
                url=_normalize_text(getattr(driver, "current_url", "")),
                screenshot=screenshot,
                html=html,
            )
        checkpoint["status"] = "failed"
        checkpoint["completed_at"] = _utc_now()
        checkpoint["error"] = fatal_error
        checkpoint["processed_service_type_ids"] = list(processed_ids)
        checkpoint["failed_service_type_ids"] = list(failed_ids)
        checkpoint["total_variant_rows"] = len(all_variants)
        _update_checkpoint(run_id, checkpoint)
        raise RuntimeError(f"Service Type variant extraction failed: {fatal_error}") from exc
    finally:
        try:
            recorder.save()
        except Exception as save_exc:
            log_message(f"Warning: could not persist diagnostics recorder output: {save_exc}")
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


# =============================================================================
# LEGACY STUBS (backward compatibility for UI imports)
# =============================================================================


def load_discovery_latest(path: Path = None) -> List[Dict[str, str]]:
    """Load latest variant data. Legacy name kept for backward compatibility."""
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


def discover_appointment_item_numbers(**kwargs) -> Dict[str, object]:
    """Legacy stub — redirects to extract_service_type_variants().

    Translates old parameter names to new ones so existing UI code
    continues to work without modification.
    """
    # Map old parameter names to new ones
    new_kwargs = {}
    new_kwargs["headless"] = kwargs.get("headless", True)

    probe = kwargs.get("probe_client_id") or kwargs.get("probe_client_ids")
    if probe:
        new_kwargs["probe_client_ids"] = probe
    else:
        new_kwargs["probe_client_ids"] = []

    new_kwargs["on_progress"] = kwargs.get("on_progress")
    new_kwargs["on_event"] = kwargs.get("on_event")
    new_kwargs["on_row"] = kwargs.get("on_row")
    new_kwargs["resume"] = kwargs.get("resume", True)
    new_kwargs["force_refresh"] = kwargs.get("force_refresh", False)
    new_kwargs["smoke_mode"] = kwargs.get("smoke_mode", False)
    new_kwargs["smoke_service_type_id"] = kwargs.get("smoke_service_type_id", "")
    new_kwargs["smoke_service_type_label"] = kwargs.get("smoke_service_type_label", "")

    result = extract_service_type_variants(**new_kwargs)

    # Map new result keys to old ones for backward compatibility
    output_paths = result.get("output_paths", {})
    result["row_count"] = result.get("total_variant_rows", 0)
    result["rows"] = []
    result["diagnostics_folder"] = output_paths.get("diagnostics_dir", "")
    result["discovery_latest_csv"] = output_paths.get("latest_csv", "")
    result["discovery_latest_xlsx"] = output_paths.get("latest_xlsx", "")
    result["output_root"] = str(Path(output_paths.get("latest_csv", "")).parent) if output_paths.get("latest_csv") else ""

    return result


def run_service_type_merge(discovered_rows: List[Dict[str, str]], **kwargs) -> Dict[str, object]:
    """Legacy stub — merge is no longer needed.

    The new variant extractor already produces complete Rate+Code data
    directly from the Assist table. This stub returns an empty result
    to keep the UI merge button from crashing.
    """
    progress = kwargs.get("progress")
    if progress:
        progress("Merge is no longer needed — variant extraction already includes Rate+Code per variant.")

    return {
        "enriched_count": 0,
        "unmatched_count": 0,
        "enriched_rows": [],
        "enriched_latest_csv": "",
        "unmatched_latest_csv": "",
    }

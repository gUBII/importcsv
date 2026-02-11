import csv
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from importcsv import (
    log_message,
    ensure_credentials,
    normalize_label,
    sanitize_csv_value,
)
from selenium_helpers import retry, wait_for, click_js
from worker_state import (
    reserve_worker_sequence,
    record_worker_event,
    get_worker_last_purge,
    reset_worker_state,
)

BASE_URL = "https://tp1.com.au/"
CARERS_PAGE_URL = f"{BASE_URL.rstrip('/')}/carers.asp?posted=yes&fld57=False"
ARCHIVE_ROOT = Path(
    os.getenv("PURGED_WORKER_ROOT", str(Path.home() / "PurgedWorker"))
).expanduser().resolve()
DOWNLOADS_DIR = ARCHIVE_ROOT / "_downloads"
LATEST_WORKER_EXCEL = ARCHIVE_ROOT / "latest_workers.xlsx"
WORKER_MANIFEST_PATH = ARCHIVE_ROOT / "worker_manifest.csv"
DEFAULT_RECORD_LIMIT = 10000

WORKER_UNIVERSAL_ID = None
FILE_PREFIX = ""
OUTPUT_DIR = None
FINAL_OUTPUT_DIR = None
DOCUMENTS_DIR = None
LOG_SINK = None
OPERATOR_NAME = None
DOWNLOAD_TIMEOUT = 60


def sanitize_component(value, fallback="Worker"):
    text = (value or fallback).strip()
    if not text:
        text = fallback
    safe = re.sub(r"[^\w\s-]", "_", text)
    safe = re.sub(r"\s+", "_", safe).strip("_")
    return safe or fallback


def _format_folder_name(worker_id, worker_name):
    safe_name = sanitize_component(worker_name or "Worker")
    return f"{FILE_PREFIX}{safe_name} {worker_id}"


def ensure_worker_root():
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    return ARCHIVE_ROOT


def snapshot_files(folder: Path):
    ensure_worker_root()
    folder.mkdir(parents=True, exist_ok=True)
    return {p.name for p in folder.iterdir() if p.is_file()}


def wait_for_new_file_in(folder: Path, previous: set[str], timeout=DOWNLOAD_TIMEOUT):
    folder.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        ready = [
            p
            for p in folder.iterdir()
            if p.is_file() and not p.name.endswith(".crdownload") and p.name not in previous
        ]
        if ready:
            ready.sort(key=lambda f: f.stat().st_mtime, reverse=True)
            return ready[0]
        time.sleep(0.5)
    raise TimeoutException("Timed out waiting for download to finish.")


def build_chrome_driver(headless=False, download_dir=None):
    target_dir = download_dir or OUTPUT_DIR
    if target_dir is None:
        raise RuntimeError("Output directory not configured before creating driver.")

    chrome_options = webdriver.ChromeOptions()
    prefs = {
        "download.default_directory": str(target_dir),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
    }
    chrome_options.add_experimental_option("prefs", prefs)
    chrome_options.add_argument("--disable-notifications")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--window-size=1400,900")
    if headless:
        chrome_options.add_argument("--headless=new")
    return webdriver.Chrome(options=chrome_options)


def assign_worker_sequence(universal_id):
    global WORKER_UNIVERSAL_ID, FILE_PREFIX
    WORKER_UNIVERSAL_ID = str(universal_id)
    FILE_PREFIX = f"{WORKER_UNIVERSAL_ID} "


def configure_worker_context(worker_id, worker_name=None):
    global OUTPUT_DIR, FINAL_OUTPUT_DIR, DOCUMENTS_DIR
    if not FILE_PREFIX:
        raise RuntimeError("Universal worker sequence is not initialized.")
    ensure_worker_root()
    OUTPUT_DIR = (ARCHIVE_ROOT / f"{FILE_PREFIX}{worker_id}").resolve()
    DOCUMENTS_DIR = OUTPUT_DIR / f"{WORKER_UNIVERSAL_ID} Documents"
    initial_name = worker_name or "Worker"
    FINAL_OUTPUT_DIR = (ARCHIVE_ROOT / _format_folder_name(worker_id, initial_name)).resolve()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)


def update_final_worker_name(worker_id, new_name):
    global FINAL_OUTPUT_DIR
    cleaned = normalize_label(new_name or "") or "Worker"
    FINAL_OUTPUT_DIR = (ARCHIVE_ROOT / _format_folder_name(worker_id, cleaned)).resolve()


def finalize_output_directory():
    global OUTPUT_DIR, FINAL_OUTPUT_DIR, DOCUMENTS_DIR
    if not FINAL_OUTPUT_DIR or OUTPUT_DIR is None:
        return
    ensure_worker_root()
    if OUTPUT_DIR == FINAL_OUTPUT_DIR:
        return
    target = FINAL_OUTPUT_DIR
    if target.exists() and target != OUTPUT_DIR:
        shutil.rmtree(target)
    if OUTPUT_DIR.exists():
        try:
            OUTPUT_DIR.rename(target)
        except OSError as exc:
            if exc.errno == 18:  # cross-device link
                shutil.copytree(OUTPUT_DIR, target, dirs_exist_ok=True)
                shutil.rmtree(OUTPUT_DIR)
            else:
                raise
    OUTPUT_DIR = target
    FINAL_OUTPUT_DIR = target
    DOCUMENTS_DIR = OUTPUT_DIR / f"{WORKER_UNIVERSAL_ID} Documents"


def calculate_directory_bytes(path: Path | None) -> int:
    if not path or not path.exists():
        return 0
    total = 0
    for file in path.rglob("*"):
        if file.is_file():
            total += file.stat().st_size
    return total


def guard_against_duplicate(worker_id):
    record = get_worker_last_purge(worker_id)
    if not record:
        return
    timestamp = record.get("timestamp", "unknown")
    human = timestamp
    try:
        human = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    log_message(
        f"Worker {worker_id} already purged on {human}. Skipping duplicate."
    )
    raise RuntimeError(f"Worker {worker_id} already purged on {human}.")


def _set_record_limit(driver, limit=DEFAULT_RECORD_LIMIT):
    try:
        select_elem = wait_for(driver, By.NAME, "psize", timeout=5)
    except NoSuchElementException:
        log_message("Worker page size selector not found.")
        return False

    dropdown = Select(select_elem)
    preferred_values = {str(limit), "10000", "5000", "2500", "1000"}
    for value in preferred_values:
        try:
            dropdown.select_by_value(value)
            log_message(f"Worker page size set to {value}.")
            return True
        except Exception:
            continue
    # force-set via JavaScript when option not present
    try:
        driver.execute_script(
            "arguments[0].value=arguments[1]; arguments[0].dispatchEvent(new Event('change'));",
            select_elem,
            str(limit),
        )
        log_message(f"Worker page size forced to {limit} via script.")
        return True
    except Exception:
        pass
    log_message("Unable to adjust worker page size; continuing with default.")
    return False


def _open_search_options(driver):
    selectors = [
        "//a[contains(translate(text(),'SEARCH','search'),'search options')]",
        "//button[contains(translate(text(),'SEARCH','search'),'search options')]",
        "//input[@type='button' and contains(translate(@value,'SEARCH','search'),'search')]",
    ]
    for xpath in selectors:
        try:
            btn = driver.find_element(By.XPATH, xpath)
            click_js(driver, btn)
            time.sleep(0.4)
            return True
        except Exception:
            continue
    return False


def _submit_worker_search(driver):
    search_selectors = [
        "//input[@type='submit' and contains(translate(@value,'SEARCH','search'),'search')]",
        "//button[contains(translate(text(),'SEARCH','search'),'search')]",
    ]
    def _attempt_click():
        for xpath in search_selectors:
            try:
                button = driver.find_element(By.XPATH, xpath)
                click_js(driver, button)
                log_message("Worker search triggered.")
                return True
            except Exception:
                continue
        raise RuntimeError("Worker search button not found.")

    try:
        return retry(_attempt_click, attempts=2, delay=0.5)
    except Exception:
        log_message("Worker search button not found.")
        return False


WORKER_LINK_XPATH = "//a[contains(@href,'carer-details.asp') and contains(@href,'eid=')]"


def _extract_worker_rows(elements):
    entries: List[Dict[str, str]] = []
    for element in elements:
        href = (element.get_attribute("href") or "").strip()
        if not href:
            continue
        match = re.search(r"eid=(\d+)", href)
        if not match:
            continue
        worker_id = match.group(1)
        name = (element.text or "").strip()
        team = ""
        try:
            row = element.find_element(By.XPATH, "./ancestor::tr[1]")
            cells = row.find_elements(By.XPATH, "./td")
            if len(cells) >= 3:
                team = cells[2].text.strip()
        except Exception:
            team = ""
        entries.append(
            {
                "worker_id": worker_id,
                "full_name": name,
                "team": team,
                "details_url": href,
            }
        )
    return entries


def _write_worker_manifest(entries: Iterable[Dict[str, str]], manifest_path: Path):
    ensure_worker_root()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Order", "Worker ID", "Full Name", "Team", "Details URL"])
        for index, entry in enumerate(entries, start=1):
            writer.writerow(
                [
                    index,
                    entry.get("worker_id", ""),
                    entry.get("full_name", ""),
                    entry.get("team", ""),
                    entry.get("details_url", ""),
                ]
            )
    return manifest_path


def collect_workers(headless=False, limit=DEFAULT_RECORD_LIMIT, manifest_path=None):
    target_manifest = manifest_path or WORKER_MANIFEST_PATH
    driver = build_chrome_driver(headless=headless, download_dir=DOWNLOADS_DIR)
    entries: List[Dict[str, str]] = []
    seen_ids = set()
    try:
        ensure_credentials()
        from importcsv import login  # late import to avoid circular issues

        login(driver)
        driver.get(CARERS_PAGE_URL)
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        _open_search_options(driver)
        _set_record_limit(driver, limit)
        _submit_worker_search(driver)
        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.XPATH, WORKER_LINK_XPATH))
            )
        except TimeoutException:
            log_message("No worker links found after search.")
            return {"manifest_path": Path(target_manifest), "count": 0}
        elements = driver.find_elements(By.XPATH, WORKER_LINK_XPATH)
        rows = _extract_worker_rows(elements)
        for entry in rows:
            worker_id = entry.get("worker_id")
            if not worker_id or worker_id in seen_ids:
                continue
            seen_ids.add(worker_id)
            entries.append(entry)
    finally:
        driver.quit()

    if not entries:
        log_message("No workers discovered on carers page.")
    else:
        _write_worker_manifest(entries, Path(target_manifest))
        log_message(
            f"Worker collector saved {len(entries)} worker(s) to {target_manifest}."
        )
    return {
        "manifest_path": Path(target_manifest),
        "count": len(entries),
    }


def _trigger_excel_download(driver):
    def _find():
        return WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    "//a[contains(translate(text(),'EXCEL','excel'),'excel') or contains(@title,'Excel') "
                    "or contains(@onclick,'generateXL')]"
                    " | //button[contains(translate(text(),'EXCEL','excel'),'excel')]"
                    " | //img[@alt='Excel']/parent::a[contains(@onclick,'generateXL')]",
                )
            )
        )

    button = retry(_find, attempts=3, delay=1.0)
    driver.execute_script("arguments[0].scrollIntoView(true);", button)
    click_js(driver, button)


def download_worker_excel(headless=False, limit=DEFAULT_RECORD_LIMIT):
    ensure_worker_root()
    driver = build_chrome_driver(headless=headless, download_dir=DOWNLOADS_DIR)
    try:
        ensure_credentials()
        from importcsv import login

        login(driver)
        driver.get(CARERS_PAGE_URL)
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        _open_search_options(driver)
        _set_record_limit(driver, limit)
        _submit_worker_search(driver)
        previous = snapshot_files(DOWNLOADS_DIR)
        _trigger_excel_download(driver)
        downloaded = wait_for_new_file_in(DOWNLOADS_DIR, previous)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = f"workers_{timestamp}.xlsx"
        final_path = DOWNLOADS_DIR / safe_name
        downloaded.rename(final_path)
        if LATEST_WORKER_EXCEL.exists():
            LATEST_WORKER_EXCEL.unlink()
        shutil.copy2(final_path, LATEST_WORKER_EXCEL)
        log_message(f"Worker Excel downloaded -> {final_path.name}")
        return final_path
    finally:
        driver.quit()


def _extract_form_fields(driver):
    """
    Scrape form inputs/selects/textareas in read-only fashion, pairing values to the
    nearest label (or name/id when labels are absent). Captures select text and checkbox state.
    """
    fields: Dict[str, str] = {}
    checks: Dict[str, bool] = {}
    radio_groups: Dict[str, bool] = {}

    def register_value(label, value):
        label = normalize_label(label)
        if not label:
            return
        if label in fields:
            suffix = 2
            candidate = f"{label} ({suffix})"
            while candidate in fields:
                suffix += 1
                candidate = f"{label} ({suffix})"
            fields[candidate] = value or ""
        else:
            fields[label] = value or ""

    def lookup_label(elem):
        aria = (elem.get_attribute("aria-label") or "").strip()
        if aria:
            return aria
        elem_id = elem.get_attribute("id") or ""
        elem_name = elem.get_attribute("name") or ""
        if elem_id:
            try:
                lab = driver.find_element(By.XPATH, f"//label[@for='{elem_id}']")
                if lab.text.strip():
                    return lab.text.strip()
            except Exception:
                pass
        # walk up to nearest row/cell and use the first text cell as label fallback
        try:
            row = elem.find_element(By.XPATH, "./ancestor::tr[1]")
            cells = row.find_elements(By.XPATH, "./th|./td")
            if cells:
                for cell in cells:
                    txt = cell.text.strip()
                    if txt and cell != cells[-1]:
                        return txt
        except Exception:
            pass
        placeholder = elem.get_attribute("placeholder") or ""
        if placeholder.strip():
            return placeholder.strip()
        if elem_name.strip():
            return elem_name.strip()
        if elem_id.strip():
            return elem_id.strip()
        return ""

    inputs = driver.find_elements(By.XPATH, "//input|//select|//textarea")
    for elem in inputs:
        tag = elem.tag_name.lower()
        input_type = (elem.get_attribute("type") or "").lower()
        label = lookup_label(elem)
        if not label:
            continue
        try:
            if tag == "select":
                selected = elem.find_elements(By.XPATH, "./option[@selected]")
                if selected:
                    register_value(label, selected[0].text.strip())
                else:
                    register_value(label, elem.get_attribute("value") or "")
            elif input_type == "radio":
                group_key = _normalize_key(elem.get_attribute("name") or label)
                if group_key not in radio_groups:
                    radio_groups[group_key] = None
                if elem.is_selected():
                    val = (elem.get_attribute("value") or "").strip().lower()
                    if val in ("yes", "y", "true", "1"):
                        radio_groups[group_key] = True
                    elif val in ("no", "n", "false", "0"):
                        radio_groups[group_key] = False
                    else:
                        radio_groups[group_key] = True
            elif input_type == "checkbox":
                checks[label] = elem.is_selected()
            elif input_type == "checkbox":
                checks[label] = elem.is_selected()
            else:
                register_value(label, (elem.get_attribute("value") or elem.text or "").strip())
        except Exception:
            continue
    # fold radio groups into checks
    for group, state in radio_groups.items():
        if state is not None:
            checks[group] = state

    # combine split date fields (e.g., Date of Birth day/month/year selects)
    dob_parts = []
    for key, value in fields.items():
        if _normalize_key(key).startswith("dateofbirth"):
            if value:
                dob_parts.append(value.strip())
    if dob_parts:
        combined = " ".join(dob_parts)
        fields["Date of Birth"] = combined

    return fields, checks


def _normalize_key(key: str) -> str:
    if not key:
        return ""
    text = normalize_label(key).lower()
    return re.sub(r"[^a-z0-9]+", "", text)


QUALIFICATIONS: List[Tuple[str, str]] = [
    ("Police check", "Police check"),
    ("Covid - 19 Vaccination", "Covid - 19 Vaccination"),
    ("NDISWC", "NDISWC"),
    ("First Aid", "First Aid"),
    ("NSW Drivers license", "NSW Drivers license"),
    ("CPR", "CPR"),
    ("International Drivers License", "International Drivers License"),
    ("WWCC", "WWCC"),
    ("NDIS Orientation Module", "NDIS Orientation Module"),
    ("APRHA Registration", "APRHA Registration"),
    ("COVID Safe Module", "COVID Safe Module"),
    ("Certificate IV Aged Care", "Certificate IV Aged Care"),
    ("NDIS Supporting Safe and Enjoyable Meals Module", "NDIS Supporting Safe and Enjoyable Meals Module"),
    ("NDIS Supporting Effective Communication Module", "NDIS Supporting Effective Communication Module"),
    ("NDIS Induction Module", "NDIS Induction Module"),
    ("SSRC - SAFE-Space e-Learning - Keeping Children Safe In Organisations In Disability Sector", "SSRC - SAFE-Space e-Learning - Keeping Children Safe In Organisations In Disability Sector"),
    ("SSRC - Child-Safe e-Learning - Keeping Children Safe In Organisations", "SSRC - Child-Safe e-Learning - Keeping Children Safe In Organisations"),
    ("Personal Care", "Personal Care"),
    ("Certificate III Disability support", "Certificate III Disability support"),
    ("Certificate IV Disability support", "Certificate IV Disability support"),
    ("Diploma in Disability support", "Diploma in Disability support"),
    ("Certificate IV community services", "Certificate IV community services"),
    ("Diploma in Community Services", "Diploma in Community Services"),
    ("Diploma in Enrolled Nursing", "Diploma in Enrolled Nursing"),
    ("Bachelor of Nursing", "Bachelor of Nursing"),
    ("Certificate III in Business Administration", "Certificate III in Business Administration"),
    ("Certificate IV in Business Administration", "Certificate IV in Business Administration"),
    ("Diploma in Business Administration", "Diploma in Business Administration"),
    ("Advanced Dip Leadership and Management", "Advanced Dip Leadership and Management"),
    ("Admin Only", "Admin Only"),
]

ALLOWANCES = [
    "Personal Mobile",
    "Work Mobile",
    "Home Office",
]


def _pick_value(data, aliases: List[str]):
    normalized = { _normalize_key(k): v for k, v in data.items() if k }
    for key in aliases:
        token = _normalize_key(key)
        if token in normalized and normalized[token]:
            return normalized[token]
    return ""


def _pick_yes_no(checks, aliases: List[str]):
    normalized = {_normalize_key(k): v for k, v in checks.items() if k}
    for key in aliases:
        token = _normalize_key(key)
        if token in normalized:
            val = normalized[token]
            if isinstance(val, str):
                val_l = val.strip().lower()
                if val_l in ("yes", "true", "1", "y"):
                    return "Yes"
                if val_l in ("no", "false", "0", "n"):
                    return "No"
            return "Yes" if bool(val) else "No"
    return ""


def _normalize_date(text):
    return (text or "").strip()


def _build_worker_payload(fields, checks, worker_id, provided_name=None, worker_team=None):
    first = _pick_value(fields, ["First Name", "First name", "fname", "first_name"])
    last = _pick_value(fields, ["Surname", "Last Name", "lname", "last_name"])
    full_name = provided_name or " ".join(part for part in [first, last] if part).strip()

    data = {
        "Worker ID": worker_id,
        "Title": _pick_value(fields, ["Title"]),
        "First Name": first,
        "Surname": last,
        "Gender": _pick_value(fields, ["Gender"]),
        "Email": _pick_value(fields, ["Email"]),
        "User Level": _pick_value(fields, ["User Level", "User level"]),
        "Pay Group": _pick_value(fields, ["Pay Group", "Pay group"]),
        "Pay Level": _pick_value(fields, ["Pay level", "Pay Level", "Paylevel"]),
        "Team": _pick_value(fields, ["Team", "Teams", "Team(s)"]) or (worker_team or ""),
        "Accounting System Reference": _pick_value(fields, ["Accounting System Reference", "Accounting Reference"]),
        "ABN / Contractor Number": _pick_value(fields, ["ABN / Contractor Number", "ABN", "Contractor Number"]),
        "Case Manager Account": _pick_yes_no(checks, ["Case Manager Account", "Case Manager"]),
        "Language Spoken": _pick_value(fields, ["Languages Spoken", "Language Spoken"]),
        "Address": _pick_value(fields, ["Address"]),
        "Suburb": _pick_value(fields, ["Suburb"]),
        "Postcode": _pick_value(fields, ["Postcode"]),
        "State": _pick_value(fields, ["State"]),
        "Landline": _pick_value(fields, ["Landline", "Phone", "Home Phone"]),
        "Mobile": _pick_value(fields, ["Mobile", "Mobile Phone"]),
        "Emergency Contact Name": _pick_value(fields, ["Emergency Contact Name"]),
        "Emergency Contact Phone": _pick_value(fields, ["Emergency Contact Phone"]),
        "Notify SMS": _pick_yes_no(checks, ["Notify SMS"]),
        "Notify Email": _pick_yes_no(checks, ["Notify Email"]),
        "Get Roster Notifications": _pick_yes_no(checks, ["Get Roster Notifications", "Roster Notifications"]),
        "Date of Birth": _normalize_date(_pick_value(fields, ["Date of Birth", "DOB"])),
        "Notes": _pick_value(fields, ["Notes"]),
        "Rostering Notes": _pick_value(fields, ["Rostering Notes"]),
        "Ignore Conflicts": _pick_yes_no(checks, ["Ignore Conflicts"]),
        "Ignore Award Alerts": _pick_yes_no(checks, ["Ignore Award Alerts"]),
        "Care Worker": _pick_yes_no(checks, ["Care Worker"]),
        "System-Only User": _pick_yes_no(checks, ["System-Only User"]),
        "Enable GPS Tracking": _pick_yes_no(checks, ["Enable GPS Tracking"]),
        "Mobile PIN Required": _pick_yes_no(checks, ["Mobile PIN Required"]),
        "Mobile PIN": _pick_value(fields, ["Mobile PIN"]),
        "Travel Pay Settings": _pick_value(fields, ["Travel Pay Settings", "Travel Pay"]),
        "Min Hours Per week": _pick_value(fields, ["Min Hours Per week", "Min Hours Per Week"]),
        "Max Hours Per Week": _pick_value(fields, ["Max Hours Per Week"]),
        "Employment Start Date": _normalize_date(_pick_value(fields, ["Employment Start Date", "Start Date"])),
        "Employment End Date": _normalize_date(_pick_value(fields, ["Employment End Date", "End Date"])),
        "Work sites / Location": _pick_yes_no(checks, ["Work sites / Location", "Work sites", "Location"]),
        "Geographic Region": _pick_value(fields, ["Geographic Region"]),
        "Full Name": full_name,
    }
    return data, full_name


def _write_csv(filename: Path, records: List[Dict[str, str]]):
    if not records:
        records = [{}]
    headers: List[str] = []
    for record in records:
        for key in record.keys():
            if key not in headers:
                headers.append(key)
    with filename.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if not headers:
            writer.writerow([])
        else:
            writer.writerow(headers)
            for record in records:
                writer.writerow([sanitize_csv_value(record.get(h, "")) for h in headers])


def _write_qualification_csv(prefix_path: Path, fields, checks):
    rows = []
    for label, key in QUALIFICATIONS:
        tick = _pick_yes_no(checks, [label, key])
        date = _normalize_date(_pick_value(fields, [f"{label} Date", f"{key} Date", label]))
        rows.append(
            {
                "Certification": label,
                "Ticked": tick or "",
                "Date": date,
            }
        )
    _write_csv(prefix_path / f"{FILE_PREFIX}Qualification.csv", rows)


def _write_allowance_csv(prefix_path: Path, checks):
    rows = []
    for label in ALLOWANCES:
        rows.append(
            {
                "Allowance": label,
                "Ticked": _pick_yes_no(checks, [label]),
            }
        )
    _write_csv(prefix_path / f"{FILE_PREFIX}Allowance.csv", rows)


def run_worker_purge(worker_id, worker_name=None, worker_team=None, headless=False):
    """
    Execute the extraction flow for a worker ID.
    Duplicate workers are skipped without override.
    """
    guard_against_duplicate(worker_id)
    universal_slot, purged_so_far = reserve_worker_sequence()
    assign_worker_sequence(universal_slot)
    log_message(
        f"[Worker] Universal sequence {WORKER_UNIVERSAL_ID} armed. "
        f"{purged_so_far} worker(s) purged so far."
    )

    configure_worker_context(worker_id, worker_name)
    run_worker_purge.current_worker_id = worker_id  # type: ignore[attr-defined]

    driver = build_chrome_driver(headless=headless)
    success = False
    final_name = worker_name or ""

    try:
        ensure_credentials()
        from importcsv import login, set_operator_name, OPERATOR_NAME  # noqa: PLC0415

        set_operator_name(OPERATOR_NAME)
        login(driver)

        edit_url = f"{BASE_URL.rstrip('/')}/user-edit.asp?eid={worker_id}"
        driver.get(edit_url)
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "body")))

        fields, checks = _extract_form_fields(driver)
        payload, final_name = _build_worker_payload(
            fields, checks, worker_id, worker_name, worker_team=worker_team
        )
        update_final_worker_name(worker_id, final_name or worker_name)
        main_csv = OUTPUT_DIR / f"{FILE_PREFIX}WorkerDetail.csv"
        _write_csv(main_csv, [payload])
        _write_qualification_csv(OUTPUT_DIR, fields, checks)
        _write_allowance_csv(OUTPUT_DIR, checks)
        log_message(f"[Worker] Extracted profile for {payload.get('Full Name') or worker_id}")
        success = True
    finally:
        driver.quit()
        finalize_output_directory()

    if success:
        archive_bytes = calculate_directory_bytes(FINAL_OUTPUT_DIR)
        timestamp_iso = datetime.now(timezone.utc).isoformat()
        state = record_worker_event(
            universal_id=WORKER_UNIVERSAL_ID,
            worker_id=worker_id,
            worker_name=final_name,
            success=True,
            bytes_written=archive_bytes,
            timestamp_iso=timestamp_iso,
            operator=OPERATOR_NAME,
        )
        log_message(f"[Worker] Purging complete. Files saved to {FINAL_OUTPUT_DIR}")
        log_message(
            f"[Worker] Counters updated -> total {state['purged_count']} | "
            f"next universal slot {state['next_universal_id']}"
        )
    return FINAL_OUTPUT_DIR


def reset_worker_data():
    """Delete the PurgedWorker archive and reset worker counters."""
    errors = []
    try:
        if ARCHIVE_ROOT.exists():
            shutil.rmtree(ARCHIVE_ROOT)
    except Exception as exc:
        errors.append(f"archive removal failed: {exc}")
    try:
        reset_worker_state()
    except Exception as exc:
        errors.append(f"state reset failed: {exc}")
    if errors:
        raise RuntimeError("; ".join(errors))
    log_message("Worker archives and counters reset. Fresh start armed.")
    return True


def load_worker_manifest(manifest_path=None):
    path = Path(manifest_path or WORKER_MANIFEST_PATH).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Worker manifest not found at {path}")
    entries = []
    with path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            worker_id = (raw.get("Worker ID") or raw.get("worker_id") or "").strip()
            if not worker_id:
                continue
            entries.append(
                {
                    "worker_id": worker_id,
                    "full_name": raw.get("Full Name") or raw.get("full_name") or "",
                    "team": raw.get("Team") or raw.get("team") or "",
                }
            )
    if not entries:
        raise ValueError(f"No workers discovered in manifest {path}.")
    return entries


def run_worker_batch(entries, *, headless=False):
    completed = []
    for entry in entries:
        worker_id = entry["worker_id"]
        worker_name = entry.get("full_name") or None
        try:
            log_message(f"[Worker] Engaging worker {worker_id} [{entry.get('team')}]")
            output_dir = run_worker_purge(
                worker_id,
                worker_name=worker_name,
                worker_team=entry.get("team"),
                headless=headless,
            )
            completed.append(
                {
                    "worker_id": worker_id,
                    "status": "completed",
                    "path": output_dir,
                }
            )
        except Exception as exc:
            log_message(f"[Worker] Error on {worker_id}: {exc}")
            completed.append(
                {
                    "worker_id": worker_id,
                    "status": "failed",
                    "path": None,
                    "error": str(exc),
                }
            )
    return completed

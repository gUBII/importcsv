import csv
import os
import re
import time
import shutil
from pathlib import Path
from datetime import datetime, timezone
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from dotenv import load_dotenv

from purger_state import (
    reserve_universal_sequence,
    record_purge_event,
    get_client_last_purge,
    reset_state,
)

# Load credentials from .env file
load_dotenv()
TP_USERNAME = os.getenv("TP_USERNAME")
TP_PASSWORD = os.getenv("TP_PASSWORD")
CONTACT_EMAIL = os.getenv("PURGER_CONTACT_EMAIL", "ops@nexix365.com")

BASE_URL = "https://tp1.com.au/"
CLIENT_ID = "56851"
CLIENT_NAME = "KHAIR Adam"
APP_VERSION = "1.0.0"
ARCHIVE_ROOT = Path(
    os.getenv("PURGED_ARCHIVE_ROOT", str(Path.home() / "PurgedClients"))
).expanduser().resolve()
DUPLICATE_REPORTS_DIR = ARCHIVE_ROOT / "_duplicate_reports"
OPERATOR_NAME = None
RUNTIME_USERNAME = TP_USERNAME
RUNTIME_PASSWORD = TP_PASSWORD
UNIVERSAL_CLIENT_ID = None
FILE_PREFIX = ""
OUTPUT_DIR = None
DOCUMENTS_DIR = None
FINAL_OUTPUT_DIR = None
DOWNLOAD_TIMEOUT = 60  # seconds
LOG_SINK = None


def set_log_sink(callback):
    """Register a callable that receives log strings."""
    global LOG_SINK
    LOG_SINK = callback


def log_message(message):
    """Send log output to the registered sink or stdout."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    text = f"[{timestamp}] {message}"
    if LOG_SINK:
        try:
            LOG_SINK(text)
        except Exception:
            print(text)
    else:
        print(text)


class DuplicateClientError(Exception):
    """Raised when the operator attempts to purge a client that already has a record."""

    def __init__(self, client_id, record, report_path):
        self.client_id = str(client_id)
        self.record = record or {}
        self.report_path = Path(report_path) if report_path else None
        message = (
            f"Client {self.client_id} was last purged at "
            f"{self.record.get('timestamp', 'unknown')}."
        )
        super().__init__(message)


def assign_universal_sequence(universal_id):
    """Set the universal client ID/prefix for the current purge run."""
    global UNIVERSAL_CLIENT_ID, FILE_PREFIX
    UNIVERSAL_CLIENT_ID = str(universal_id)
    FILE_PREFIX = f"{UNIVERSAL_CLIENT_ID} "


def ensure_archive_root():
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    DUPLICATE_REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def format_timestamp(ts):
    if not ts:
        return "unknown"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ts


def create_duplicate_report(client_id, record):
    """Persist a CSV notice describing the last purge timestamp."""
    ensure_archive_root()
    filename = f"{client_id}_duplicate_notice.csv"
    path = DUPLICATE_REPORTS_DIR / filename
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["TurnPointID", "Time of Purge"])
        writer.writerow([client_id, record.get("timestamp", "")])
    return path


def get_duplicate_metadata(client_id):
    """Expose the last purge record for external consumers (e.g., GUI)."""
    return get_client_last_purge(client_id)


def calculate_directory_bytes(path: Path | None) -> int:
    if not path or not path.exists():
        return 0
    total = 0
    for file in path.rglob("*"):
        if file.is_file():
            total += file.stat().st_size
    return total


def prompt_operator_name():
    """Ask for the operator codename once per session."""
    global OPERATOR_NAME
    if OPERATOR_NAME:
        return OPERATOR_NAME
    default = os.getenv("TP_OPERATOR") or "Operator Zero"
    try:
        response = input(f"Identify yourself [{default}]: ").strip()
    except EOFError:
        response = ""
    OPERATOR_NAME = response or default
    log_message(
        f"Thanks for using my Middleware, {OPERATOR_NAME}. "
        "This time I'm not charging you ;)"
    )
    return OPERATOR_NAME


def set_operator_name(name):
    """Allow the GUI to inject the operator name without CLI prompt."""
    global OPERATOR_NAME
    cleaned = (name or "").strip()
    if cleaned:
        OPERATOR_NAME = cleaned
    return OPERATOR_NAME


def reset_purge_data():
    """Delete the PurgedClients archive and reset purge counters."""
    errors = []
    try:
        if ARCHIVE_ROOT.exists():
            shutil.rmtree(ARCHIVE_ROOT)
    except Exception as exc:
        errors.append(f"archive removal failed: {exc}")
    try:
        reset_state()
    except Exception as exc:
        errors.append(f"state reset failed: {exc}")
    if errors:
        raise RuntimeError("; ".join(errors))
    log_message("Purge archives and counters reset. Fresh start armed.")
    return True

def login(driver):
    """Log into TurnPoint using credentials from .env."""
    username, password = ensure_credentials()
    driver.get(BASE_URL)
    # wait for login page to load
    WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.NAME, "email"))
    )
    driver.find_element(By.NAME, "email").send_keys(username)
    driver.find_element(By.NAME, "password").send_keys(password)
    driver.find_element(By.XPATH, "//input[@type='submit']").click()
    # wait for dashboard to load
    WebDriverWait(driver, 10).until(
        EC.url_contains("/dashboard.asp")
    )

def sanitize_csv_value(value):
    if value is None:
        return ""
    return str(value).replace(",", ";").strip()


def normalize_label(text):
    """Condense whitespace, drop trailing colon, and strip NBSPs for CSV headers."""
    if not text:
        return ""
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text.rstrip(":").strip()


def clean_value(text):
    if not text:
        return ""
    return sanitize_csv_value(text.replace("\xa0", " "))


def extract_fields_on_page(driver, page_name):
    """
    Extract visible field labels and values from the current page.
    Returns a dictionary mapping labels to their captured values.
    """
    data = {}

    def store_value(key, value):
        if not key:
            return
        key = normalize_label(key)
        value = value.strip()
        if key in data:
            suffix = 2
            new_key = f"{key} ({suffix})"
            while new_key in data:
                suffix += 1
                new_key = f"{key} ({suffix})"
            key_to_use = new_key
        else:
            key_to_use = key
        data[key_to_use] = value

    labels = driver.find_elements(
        By.XPATH,
        "//label | //td[@class='label'] | //td[contains(@class,'infobox_leftcol')] | "
        "//th | //div[@class='colTitle']",
    )
    for label in labels:
        header_text = label.text.strip()
        if not header_text:
            continue
        header_text = normalize_label(header_text)
        value = ""
        try:
            parent = label.find_element(By.XPATH, "..")
            inputs = parent.find_elements(By.XPATH, ".//input|.//textarea|.//select")
            if inputs:
                elem = inputs[0]
                if elem.tag_name == "select":
                    selected = elem.find_element(By.XPATH, "./option[@selected]")
                    value = selected.text.strip()
                elif elem.tag_name in ("input", "textarea"):
                    value = (elem.get_attribute("value") or elem.text).strip()
            else:
                sibling_text = parent.text.replace(label.text, "").strip()
                value = sibling_text
        except Exception:
            value = ""
        store_value(header_text, clean_value(value))
    return data

def ensure_output_directories():
    if OUTPUT_DIR is None:
        raise RuntimeError("Output directory is not configured.")
    ensure_archive_root()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)


def configure_client_context(client_id, client_name=None):
    global CLIENT_ID, CLIENT_NAME, OUTPUT_DIR, DOCUMENTS_DIR, FINAL_OUTPUT_DIR
    if not FILE_PREFIX:
        raise RuntimeError("Universal client sequence is not initialized.")
    CLIENT_ID = client_id
    if client_name:
        CLIENT_NAME = client_name
    ensure_archive_root()
    working_folder = (ARCHIVE_ROOT / f"{FILE_PREFIX}{CLIENT_ID}").resolve()
    OUTPUT_DIR = working_folder
    DOCUMENTS_DIR = OUTPUT_DIR / f"{UNIVERSAL_CLIENT_ID} Documents"
    FINAL_OUTPUT_DIR = (ARCHIVE_ROOT / f"{FILE_PREFIX}{CLIENT_NAME} ({CLIENT_ID})").resolve()
    ensure_output_directories()


def update_final_client_name(new_name):
    global CLIENT_NAME, FINAL_OUTPUT_DIR
    if not new_name:
        return
    cleaned = normalize_label(new_name)
    if not cleaned or cleaned == CLIENT_NAME:
        return
    CLIENT_NAME = cleaned
    FINAL_OUTPUT_DIR = (ARCHIVE_ROOT / f"{FILE_PREFIX}{CLIENT_NAME} ({CLIENT_ID})").resolve()


def finalize_output_directory():
    global OUTPUT_DIR, DOCUMENTS_DIR, FINAL_OUTPUT_DIR
    if not FINAL_OUTPUT_DIR or OUTPUT_DIR is None:
        return
    ensure_archive_root()
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
    DOCUMENTS_DIR = OUTPUT_DIR / f"{UNIVERSAL_CLIENT_ID} Documents"


def prompt_client_id(prompt_text=None):
    default = CLIENT_ID
    question = prompt_text or f"Enter client ID [{default}]: "
    try:
        response = input(question).strip()
    except EOFError:
        response = ""
    return response or default


def derive_client_name_from_record(record):
    if not record:
        return None
    for key in record.keys():
        if key.startswith("Client Details - "):
            return key.replace("Client Details - ", "").strip()
    if record.get("Client Name"):
        return record["Client Name"]
    return None


def confirm_duplicate_cli(client_id, record, report_path):
    """Notify the user about a duplicate purge and ask for confirmation."""
    human_time = format_timestamp(record.get("timestamp"))
    log_message(
        f"Client {client_id} already purged on {human_time}. "
        f"Duplicate notice saved at {report_path}."
    )
    try:
        answer = input("Override and purge again? [y/N]: ").strip().lower()
    except EOFError:
        answer = ""
    return answer in ("y", "yes")


def write_csv(page, records):
    """
    Write structured records (list of dicts) to CSV using the field names as headers.
    When no records exist, writes an empty header row for traceability.
    """
    safe_page = page.replace("/", "-")
    filename = OUTPUT_DIR / f"{FILE_PREFIX}{safe_page}.csv"

    if not records:
        records = [{}]

    headers = []
    for record in records:
        for key in record.keys():
            if key not in headers:
                headers.append(key)

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not headers:
            writer.writerow([])
        else:
            writer.writerow(headers)
            for record in records:
                writer.writerow([sanitize_csv_value(record.get(h, "")) for h in headers])


def safe_filename(name):
    return re.sub(r'[\\\\/*?:"<>|]', "_", name).strip() or "Document"


def ensure_unique_path(path: Path) -> Path:
    candidate = path
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}_{counter}{path.suffix}")
        counter += 1
    return candidate


def snapshot_downloads():
    if OUTPUT_DIR is None or not OUTPUT_DIR.exists():
        return set()
    return {p.name for p in OUTPUT_DIR.iterdir() if p.is_file()}


def wait_for_new_download(previous_files, timeout=DOWNLOAD_TIMEOUT):
    if OUTPUT_DIR is None:
        raise RuntimeError("Output directory not configured for downloads.")
    deadline = time.time() + timeout
    while time.time() < deadline:
        ready_files = []
        for p in OUTPUT_DIR.iterdir():
            if not p.is_file():
                continue
            if p.name.endswith(".crdownload"):
                continue
            if p.name not in previous_files:
                ready_files.append(p)
        if ready_files:
            ready_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
            return ready_files[0]
        time.sleep(0.5)
    raise TimeoutException("Timed out waiting for download to finish.")


def cleanup_old_csvs():
    if OUTPUT_DIR is None or not FILE_PREFIX:
        return
    for csv_file in OUTPUT_DIR.glob("*.csv"):
        if not csv_file.name.startswith(FILE_PREFIX):
            csv_file.unlink(missing_ok=True)


def download_document_files(driver):
    try:
        main_window = driver.current_window_handle
    except Exception:
        return
    if DOCUMENTS_DIR is None:
        return

    doc_links = driver.find_elements(
        By.XPATH,
        "//a[contains(@href,'document-details.asp') and contains(@href,'eid=') and contains(@href,'cid=')]",
    )
    seen = set()

    for index, link in enumerate(doc_links, start=1):
        href = link.get_attribute("href")
        if not href or "add=yes" in href or href in seen:
            continue
        seen.add(href)
        title = link.text.strip() or link.get_attribute("title") or f"Document_{index}"

        driver.execute_script("window.open(arguments[0], '_blank');", href)
        driver.switch_to.window(driver.window_handles[-1])

        try:
            download_btn = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable(
                    (
                        By.XPATH,
                        "//input[@type='submit' and contains(translate(@value,'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'DOWNLOAD')]",
                    )
                )
            )
            driver.execute_script("arguments[0].scrollIntoView(true);", download_btn)
            previous = snapshot_downloads()
            driver.execute_script("arguments[0].click();", download_btn)
            downloaded_path = wait_for_new_download(previous)
            safe_name = safe_filename(title)
            target = DOCUMENTS_DIR / f"{FILE_PREFIX}{safe_name}{downloaded_path.suffix}"
            target = ensure_unique_path(target)
            downloaded_path.rename(target)
            log_message(f"Downloaded document '{title}' -> {target.name}")
        except Exception as exc:
            log_message(f"Error downloading document '{title}': {exc}")
        finally:
            driver.close()
            driver.switch_to.window(main_window)


def download_budget_excel(driver):
    try:
        export_link = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, "//a[contains(@onclick,'generateXL')]"))
        )
    except TimeoutException:
        log_message("Budget download button not found.")
        return

    driver.execute_script("arguments[0].scrollIntoView(true);", export_link)
    previous = snapshot_downloads()
    driver.execute_script("arguments[0].click();", export_link)
    try:
        downloaded_path = wait_for_new_download(previous)
    except TimeoutException as exc:
        log_message(f"Budget download failed: {exc}")
        return

    new_name = f"{FILE_PREFIX}{downloaded_path.name}"
    target = ensure_unique_path(OUTPUT_DIR / new_name)
    downloaded_path.rename(target)
    log_message(f"Saved budget export as {target.name}")

    try:
        from NDISBUDGETER import process_budget_excel

        result = process_budget_excel(target, quiet=True)
        entries = result.get("entries_exported", 0)
        entry_folder = result.get("entry_folder")
        log_message(
            f"Processed budget workbook into {entries} entry CSVs"
            f"{' at ' + str(entry_folder) if entry_folder else ''}"
        )
    except ImportError:
        log_message("NDISBUDGETER module not available; skipping budget parsing.")
    except Exception as exc:
        log_message(f"Budget parsing step failed: {exc}")

def extract_client_details(driver):
    url = f"https://tp1.com.au/client-details.asp?eid={CLIENT_ID}"
    driver.get(url)
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.LINK_TEXT, "Client Details")))
    return [extract_fields_on_page(driver, "Client-Details")]

def extract_package_schedules(driver):
    url = f"https://tp1.com.au/client-details.asp?eid={CLIENT_ID}&BREAKDOWN_SHOW_PACKAGE_SCHEDULE=yes"
    driver.get(url)
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.XPATH, "//table")))
    records = []
    rows = driver.find_elements(
        By.XPATH,
        "//tr[td[1]/a[contains(@onclick,'client-package-schedule-details.asp')]]",
    )
    for tr in rows:
        cols = tr.find_elements(By.XPATH, "./td")
        if len(cols) < 7:
            continue
        record = {
            "Package": cols[0].text.strip(),
            "Date Start": cols[2].text.strip(),
            "Date End": cols[3].text.strip(),
            "Balance": cols[4].text.strip(),
            "Locked Date": cols[5].text.strip(),
            "Case Manager": cols[6].text.strip(),
        }
        records.append(record)
    return records

def extract_notes(driver):
    url = (
        f"https://tp1.com.au/client-details.asp?eid={CLIENT_ID}"
        "&BREAKDOWN_SHOW_NOTES=yes&noteSort=date&NoteTopRestrict=no"
    )
    driver.get(url)
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.LINK_TEXT, "Notes")))

    rows = []
    note_rows = driver.find_elements(
        By.XPATH, "//tr[contains(@class,'underlined_nohover')]"
    )

    for tr in note_rows:
        link_cells = tr.find_elements(
            By.XPATH, ".//a[contains(@onclick,'note-details.asp')]"
        )
        if not link_cells:
            continue  # skip category headers and blank rows

        cells = tr.find_elements(By.XPATH, "./td")
        if not cells:
            continue

        datetime_parts = [part.strip() for part in cells[0].text.splitlines() if part.strip()]
        date = datetime_parts[0] if datetime_parts else ""
        time_ = datetime_parts[1] if len(datetime_parts) > 1 else ""
        note_meta = []
        if len(cells) > 2:
            note_meta = [part.strip() for part in cells[2].text.splitlines() if part.strip()]
        note_type = note_meta[0] if note_meta else ""
        author = note_meta[1] if len(note_meta) > 1 else (note_meta[0] if note_meta else "")
        note_body = cells[-1].text.strip()

        rows.append(
            {
                "Note Type": note_type,
                "Date": date,
                "Time": time_,
                "Author": author,
                "Note": note_body,
            }
        )

    return rows

def extract_info_sheet(driver):
    url = f"https://tp1.com.au/client-infosheet.asp?eid={CLIENT_ID}&pageStatus=edit"
    driver.get(url)
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.XPATH, "//body")))
    return [extract_fields_on_page(driver, "Info-Sheet")]

def extract_agreement(driver):
    url = f"https://tp1.com.au/client-agreement-V12.asp?eid={CLIENT_ID}"
    driver.get(url)
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.XPATH, "//body")))
    return [extract_fields_on_page(driver, "Agreement")]

def extract_contacts(driver):
    url = f"https://tp1.com.au/client-details.asp?eid={CLIENT_ID}&BREAKDOWN_SHOW_CONTACTS=yes"
    driver.get(url)
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.XPATH, "//body")))
    return [extract_fields_on_page(driver, "Contacts")]

def extract_support_plan(driver):
    url = f"https://tp1.com.au/client-support-plan-V11.asp?eid={CLIENT_ID}"
    driver.get(url)
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.XPATH, "//body")))
    return [extract_fields_on_page(driver, "Support-Plan")]

def extract_emergency_plan(driver):
    url = f"https://tp1.com.au/client-details-emergency.asp?eid={CLIENT_ID}"
    driver.get(url)
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.XPATH, "//body")))
    return [extract_fields_on_page(driver, "Emergency-Plan")]

def extract_documents(driver):
    url = f"https://tp1.com.au/client-details.asp?eid={CLIENT_ID}&BREAKDOWN_SHOW_DOCUMENTS=yes"
    driver.get(url)
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.XPATH, "//body")))
    rows = [extract_fields_on_page(driver, "Documents")]
    download_document_files(driver)
    return rows

def extract_ndis_budget(driver):
    url = f"https://tp1.com.au/ndis-service-agreement-budget.asp?eid={CLIENT_ID}"
    driver.get(url)
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.XPATH, "//body")))
    rows = [extract_fields_on_page(driver, "NDIS-Budget")]
    download_budget_excel(driver)
    return rows

def build_chrome_driver(headless=False):
    if OUTPUT_DIR is None:
        raise RuntimeError("Output directory not configured before creating driver.")

    chrome_options = webdriver.ChromeOptions()
    prefs = {
        "download.default_directory": str(OUTPUT_DIR),
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


def run_turnpoint_purge(client_id, client_name=None, headless=False):
    """
    Execute the end-to-end extraction flow for a client ID.
    Returns the final output directory path.
    """
    universal_slot, purged_so_far = reserve_universal_sequence()
    assign_universal_sequence(universal_slot)
    log_message(
        f"Universal sequence {UNIVERSAL_CLIENT_ID} armed. "
        f"{purged_so_far} client(s) purged so far."
    )
    configure_client_context(client_id, client_name)
    cleanup_old_csvs()
    driver = build_chrome_driver(headless=headless)

    pages_and_extractors = [
        ("Client-Details", extract_client_details),
        ("Package-Schedules", extract_package_schedules),
        ("Notes", extract_notes),
        ("Info-Sheet", extract_info_sheet),
        ("Agreement", extract_agreement),
        ("Contacts", extract_contacts),
        ("Support-Plan", extract_support_plan),
        ("Emergency-Plan", extract_emergency_plan),
        ("Documents", extract_documents),
        ("NDIS-Budget", extract_ndis_budget),
    ]

    log_message(f"Launching Turnpoint session for client {client_id}")
    success = False
    try:
        login(driver)

        for page_name, extractor in pages_and_extractors:
            try:
                rows = extractor(driver)
                if page_name == "Client-Details" and rows:
                    new_name = derive_client_name_from_record(rows[0])
                    update_final_client_name(new_name)
                write_csv(page_name, rows)
                log_message(f"Extracted {len(rows)} rows for {page_name}")
            except Exception as e:
                # log but continue
                log_message(f"Error extracting {page_name}: {e}")
        success = True
    finally:
        driver.quit()
        finalize_output_directory()

    if success:
        archive_bytes = calculate_directory_bytes(FINAL_OUTPUT_DIR)
        timestamp_iso = datetime.now(timezone.utc).isoformat()
        state = record_purge_event(
            universal_id=UNIVERSAL_CLIENT_ID,
            turnpoint_id=CLIENT_ID,
            client_name=CLIENT_NAME,
            success=True,
            bytes_written=archive_bytes,
            timestamp_iso=timestamp_iso,
            operator=OPERATOR_NAME,
        )
        log_message(f"Purging complete. Files saved to {FINAL_OUTPUT_DIR}")
        log_message(
            f"Purge counters updated -> total {state['purged_count']} | "
            f"next universal slot {state['next_universal_id']}"
        )
    return FINAL_OUTPUT_DIR


def configure_credentials(username=None, password=None):
    """
    Set runtime credentials for this purge session.
    Falls back to env-derived values when parameters are missing.
    """
    global RUNTIME_USERNAME, RUNTIME_PASSWORD
    if username:
        RUNTIME_USERNAME = username
    if password:
        RUNTIME_PASSWORD = password
    return RUNTIME_USERNAME, RUNTIME_PASSWORD


def ensure_credentials():
    """Raise a descriptive error if credentials are missing."""
    if not RUNTIME_USERNAME:
        raise RuntimeError("Can't do much without credentials bro ...")
    if not RUNTIME_PASSWORD:
        raise RuntimeError("TurnPoint password is missing. Set the purge password first.")
    return RUNTIME_USERNAME, RUNTIME_PASSWORD


def main():
    selected_client_id = prompt_client_id()
    run_turnpoint_purge(selected_client_id)


if __name__ == "__main__":
    main()

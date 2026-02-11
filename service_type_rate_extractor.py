import csv
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select

from importcsv import ARCHIVE_ROOT, build_chrome_driver, ensure_credentials, login, log_message
from selenium_helpers import click_js, wait_for

# Canonical source for this module. Add Appointment is intentionally not used.
SERVICE_TYPES_PAGE_URL = (
    "https://tp1.com.au/service-types.asp?posted=yes&fld586=False&psize=1000&sbmt=yes"
)
SERVICE_TYPE_DETAILS_URL_TMPL = "https://tp1.com.au/service-type-details.asp?eid={eid}"

DATASET_COLUMNS = [
    "Service Type",
    "ID",
    "Package",
    "Service Code",
    "Payroll Code",
    "Billing Type",
    "Payment Type",
    "Tax Rate",
    "Def. Rate",
    "Recur.",
    "Rules Tag",
    "Deleted",
    "ServiceTypeLink",
]

ProgressCallback = Optional[Callable[[str], None]]
RowCallback = Optional[Callable[[Dict[str, str]], None]]


def _emit(message: str, callback: ProgressCallback = None):
    log_message(message)
    if callback:
        try:
            callback(message)
        except Exception:
            pass


def _normalize_text(value) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _normalize_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", _normalize_text(value).lower())


def _intish(value: str) -> Optional[int]:
    text = _normalize_text(value)
    if not text:
        return None
    match = re.search(r"\d+", text)
    if not match:
        return None
    try:
        return int(match.group(0))
    except Exception:
        return None


def default_rate_numeric(value: str) -> float:
    cleaned = _normalize_text(value).replace(",", "")
    cleaned = re.sub(r"[^0-9.\-]", "", cleaned)
    if not cleaned:
        return 0.0
    try:
        return float(cleaned)
    except Exception:
        return 0.0


def _normalize_deleted(value: str) -> str:
    token = _normalize_text(value).lower()
    if token in {"yes", "y", "true", "1", "deleted"}:
        return "Yes"
    if token in {"no", "n", "false", "0", "active", ""}:
        return "No"
    return _normalize_text(value)


def _extract_id_from_link(link: str) -> str:
    if not link:
        return ""
    match = re.search(r"[?&]eid=(\d+)", link)
    if match:
        return match.group(1)
    return ""


def _canonical_row(values: Dict[str, str]) -> Dict[str, str]:
    row = {column: "" for column in DATASET_COLUMNS}
    row.update({k: _normalize_text(v) for k, v in values.items()})

    row["ID"] = row["ID"] or _extract_id_from_link(row["ServiceTypeLink"])
    if row["ID"] and not row["ServiceTypeLink"]:
        row["ServiceTypeLink"] = SERVICE_TYPE_DETAILS_URL_TMPL.format(eid=row["ID"])

    row["Deleted"] = _normalize_deleted(row.get("Deleted", ""))

    return {column: _normalize_text(row.get(column, "")) for column in DATASET_COLUMNS}


def normalize_external_row(raw_row: Dict[str, str]) -> Dict[str, str]:
    """Map arbitrary input headers to final Service Types schema."""
    token_map = {
        _normalize_token(key): _normalize_text(value)
        for key, value in (raw_row or {}).items()
    }

    def pick(*aliases: str) -> str:
        for alias in aliases:
            token = _normalize_token(alias)
            if token in token_map and token_map[token] != "":
                return token_map[token]
        return ""

    mapped = {
        "Service Type": pick("Service Type", "ServiceType", "Type", "Name"),
        "ID": pick("ID", "ServiceTypeID", "Service Type ID", "EID"),
        "Package": pick("Package"),
        "Service Code": pick("Service Code", "ServiceCode", "Code"),
        "Payroll Code": pick("Payroll Code", "PayrollCode"),
        "Billing Type": pick("Billing Type", "BillingType", "Billing"),
        "Payment Type": pick("Payment Type", "PaymentType", "Payment"),
        "Tax Rate": pick("Tax Rate", "TaxRate", "Tax", "GST"),
        "Def. Rate": pick("Def. Rate", "Def Rate", "Default Rate", "DefaultRate", "Rate"),
        "Recur.": pick("Recur.", "Recur", "Recurring", "Recurrence"),
        "Rules Tag": pick("Rules Tag", "RulesTag", "Rules", "Tag"),
        "Deleted": pick("Deleted"),
        "ServiceTypeLink": pick("ServiceTypeLink", "Service Type Link", "Link", "URL", "Details URL"),
    }

    return _canonical_row(mapped)


def _ensure_paths() -> tuple[Path, Path]:
    output_root = (ARCHIVE_ROOT / "ServiceTypeRateExtractor").resolve()
    download_dir = output_root / "_downloads"
    output_root.mkdir(parents=True, exist_ok=True)
    download_dir.mkdir(parents=True, exist_ok=True)
    return output_root, download_dir


def _snapshot_files(folder: Path) -> set[str]:
    folder.mkdir(parents=True, exist_ok=True)
    return {p.name for p in folder.iterdir() if p.is_file()}


def _wait_for_new_file(folder: Path, previous: set[str], timeout: int = 90) -> Path:
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
        time.sleep(0.4)
    raise TimeoutException("Timed out waiting for Service Types export download.")


def _find_select(driver, *, aria_contains: Optional[str] = None, name: Optional[str] = None):
    selectors = []
    if aria_contains:
        selectors.extend(
            [
                (By.XPATH, f"//select[contains(translate(@aria-label,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{aria_contains.lower()}')]"),
                (By.XPATH, f"//*[@aria-label and contains(translate(@aria-label,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{aria_contains.lower()}') and self::select]"),
            ]
        )
    if name:
        selectors.extend(
            [
                (By.NAME, name),
                (By.XPATH, f"//select[@name='{name}']"),
                (By.ID, name),
                (By.XPATH, f"//select[@id='{name}']"),
            ]
        )

    for by, locator in selectors:
        elems = driver.find_elements(by, locator)
        for elem in elems:
            try:
                if elem.is_displayed():
                    return elem
            except Exception:
                continue
    return None


def _click_search(driver):
    selectors = [
        "//input[@type='submit' and contains(translate(@value,'SEARCH','search'),'search')]",
        "//button[contains(translate(normalize-space(.),'SEARCH','search'),'search')]",
        "//input[@value='SEARCH']",
    ]
    for xpath in selectors:
        elems = driver.find_elements(By.XPATH, xpath)
        for elem in elems:
            try:
                if elem.is_displayed():
                    click_js(driver, elem)
                    return True
            except Exception:
                continue
    return False


def _is_password_change_required(driver) -> bool:
    try:
        body_text = _normalize_text(driver.find_element(By.TAG_NAME, "body").text).lower()
    except Exception:
        return False
    triggers = [
        "password change required",
        "change your password",
        "update your password",
    ]
    return any(trigger in body_text for trigger in triggers)


def _is_password_change_blocking(driver) -> bool:
    """
    Detect a hard password-change gate that blocks normal navigation.

    Important: dashboard warning banners are common and should not abort capture.
    We only treat this as blocking when a visible password-reset form is present.
    """
    try:
        url = _normalize_text(getattr(driver, "current_url", "")).lower()
    except Exception:
        url = ""

    password_inputs = 0
    try:
        for field in driver.find_elements(By.XPATH, "//input[@type='password']"):
            try:
                if field.is_displayed():
                    password_inputs += 1
            except Exception:
                continue
    except Exception:
        password_inputs = 0

    if password_inputs < 2:
        return False

    if any(marker in url for marker in ("my-account", "mydetails", "change-password", "password")):
        return True

    try:
        body_text = _normalize_text(driver.find_element(By.TAG_NAME, "body").text).lower()
    except Exception:
        body_text = ""
    triggers = (
        "password change required",
        "change your password",
        "update your password",
        "new password",
        "confirm password",
    )
    return any(trigger in body_text for trigger in triggers)


def _is_authenticated(driver) -> bool:
    url = _normalize_text(getattr(driver, "current_url", "")).lower()
    if "login" in url:
        return False
    checks = [
        "//a[contains(translate(.,'LOGOUT','logout'),'logout')]",
        "//*[contains(translate(.,'LOGGED IN AS','logged in as'),'logged in as')]",
    ]
    for xpath in checks:
        try:
            elems = driver.find_elements(By.XPATH, xpath)
            if any(elem.is_displayed() for elem in elems):
                return True
        except Exception:
            continue
    return True


def _login_failure_hint(driver) -> str:
    try:
        url = _normalize_text(getattr(driver, "current_url", "")).lower()
    except Exception:
        url = ""
    if "login.asp" not in url:
        return ""

    selectors = [
        "//td[contains(@class,'red')]",
        "//*[contains(@class,'error')]",
        "//div[contains(@class,'error')]",
    ]
    for xpath in selectors:
        try:
            elems = driver.find_elements(By.XPATH, xpath)
        except Exception:
            elems = []
        for elem in elems:
            try:
                if not elem.is_displayed():
                    continue
                text = _normalize_text(elem.text)
                if text:
                    if "incorrect" in text.lower() and "password" in text.lower():
                        return "TurnPoint rejected credentials (email/password incorrect)."
                    return text
            except Exception:
                continue

    try:
        body_text = _normalize_text(driver.find_element(By.TAG_NAME, "body").text).lower()
    except Exception:
        body_text = ""
    if "incorrect" in body_text and "password" in body_text:
        return "TurnPoint rejected credentials (email/password incorrect)."
    if "login" in body_text and "required" in body_text:
        return "TurnPoint login required."
    return ""


def _navigate_to_service_types_page(driver, progress: ProgressCallback = None):
    _emit("ServiceType→Rate: navigating to service-types page.", progress)
    driver.get(SERVICE_TYPES_PAGE_URL)
    wait_for(driver, By.TAG_NAME, "body", timeout=25)
    try:
        title = _normalize_text(driver.title)
        if "service" not in title.lower():
            _emit(
                f"ServiceType→Rate warning: unexpected page title after navigation: '{title}'.",
                progress,
            )
    except Exception:
        pass


def _find_service_types_table(driver):
    tables = driver.find_elements(By.XPATH, "//table")
    for table in tables:
        try:
            if not table.is_displayed():
                continue
        except Exception:
            continue
        # TurnPoint often renders a title row first (without the real column headers),
        # so do not rely on first-row header cells only.
        header_cells = table.find_elements(By.XPATH, ".//th")
        header_tokens = [_normalize_token(cell.text) for cell in header_cells if _normalize_text(cell.text)]
        has_service_header = any(token == "servicetype" for token in header_tokens)
        has_id_header = any(token == "id" for token in header_tokens)
        if has_service_header and has_id_header:
            return table

        # Fallback: identify the data grid by details links even if header markup differs.
        detail_links = table.find_elements(
            By.XPATH,
            ".//a[contains(@href,'service-type-details.asp') and contains(@href,'eid=')]",
        )
        if detail_links:
            return table
    return None


def _wait_for_table_rows(driver, timeout: int = 45):
    deadline = time.time() + timeout
    while time.time() < deadline:
        table = _find_service_types_table(driver)
        if table is None:
            # Secondary fallback: detect visible details links first and climb to table.
            detail_links = driver.find_elements(
                By.XPATH,
                "//a[contains(@href,'service-type-details.asp') and contains(@href,'eid=')]",
            )
            for link in detail_links:
                try:
                    if not link.is_displayed():
                        continue
                    table = link.find_element(By.XPATH, "./ancestor::table[1]")
                    break
                except Exception:
                    continue
        if table is None:
            time.sleep(0.25)
            continue
        rows = table.find_elements(
            By.XPATH,
            ".//tr[td and .//a[contains(@href,'service-type-details.asp') and contains(@href,'eid=')]]",
        )
        if not rows:
            rows = table.find_elements(By.XPATH, ".//tbody/tr[td]")
        if not rows:
            rows = table.find_elements(By.XPATH, ".//tr[td]")
        if rows:
            return table
        time.sleep(0.25)
    raise TimeoutException("Service Types table rows were not detected after search.")


def _set_records_per_page(driver, progress: ProgressCallback = None) -> int:
    select_elem = _find_select(driver, aria_contains="records per page", name="psize")
    if select_elem is None:
        raise RuntimeError("Records per page dropdown was not found on Service Types page.")

    dropdown = Select(select_elem)
    numeric_options = []
    for option in dropdown.options:
        raw = option.get_attribute("value") or option.text
        match = re.search(r"\d+", _normalize_text(raw))
        if not match:
            continue
        value = int(match.group(0))
        numeric_options.append((value, option.text.strip(), raw))

    if not numeric_options:
        raise RuntimeError("Records per page dropdown has no numeric options.")

    numeric_options.sort(key=lambda item: item[0])
    target = None
    for option in numeric_options:
        if option[0] >= 1000:
            target = option
    if target is None:
        target = numeric_options[-1]

    target_value, target_text, target_raw = target
    selected = False
    try:
        dropdown.select_by_value(str(target_value))
        selected = True
    except Exception:
        pass
    if not selected:
        try:
            dropdown.select_by_value(str(target_raw))
            selected = True
        except Exception:
            pass
    if not selected:
        dropdown.select_by_visible_text(target_text)

    _emit(f"ServiceType→Rate: setting Records per page = {target_value}.", progress)
    return target_value


def _set_deleted_filter_no(driver, progress: ProgressCallback = None):
    select_elem = _find_select(driver, aria_contains="deleted", name="fld586")
    if select_elem is None:
        _emit(
            "ServiceType→Rate warning: Deleted filter dropdown not found; continuing.",
            progress,
        )
        return

    dropdown = Select(select_elem)
    chosen = None
    for option in dropdown.options:
        token = _normalize_text(option.text).lower()
        value_token = _normalize_text(option.get_attribute("value") or "").lower()
        if token in {"no", "false", "n"} or value_token in {"false", "0", "no", "n"}:
            chosen = option
            break
    if chosen is None and dropdown.options:
        # fallback to first non-empty option when exact text is unavailable
        for option in dropdown.options:
            if _normalize_text(option.text):
                chosen = option
                break
    if chosen is None:
        return

    try:
        dropdown.select_by_visible_text(chosen.text)
    except Exception:
        value = chosen.get_attribute("value") or ""
        if value:
            dropdown.select_by_value(value)

    _emit("ServiceType→Rate: applying default filter Deleted = No.", progress)


def _refresh_search_results(driver, progress: ProgressCallback = None):
    clicked = _click_search(driver)
    if not clicked:
        raise RuntimeError("SEARCH button was not found on Service Types page.")
    _emit("ServiceType→Rate: search submitted; waiting for results.", progress)
    _wait_for_table_rows(driver, timeout=45)


def _find_export_control(driver):
    selectors = [
        "//a[contains(@onclick,'generateXL')]",
        "//a[contains(translate(normalize-space(.),'EXCEL','excel'),'excel')]",
        "//button[contains(translate(normalize-space(.),'EXCEL','excel'),'excel')]",
        "//img[contains(translate(@alt,'EXCEL','excel'),'excel')]/parent::a",
        "//img[contains(translate(@src,'EXCEL','excel'),'excel')]/ancestor::a[1]",
        "//a[contains(translate(@title,'EXCEL','excel'),'excel')]",
    ]
    for xpath in selectors:
        elems = driver.find_elements(By.XPATH, xpath)
        for elem in elems:
            try:
                if elem.is_displayed():
                    return elem
            except Exception:
                continue
    return None


def _download_export_file(driver, download_dir: Path, progress: ProgressCallback = None) -> Path:
    control = _find_export_control(driver)
    if control is None:
        raise RuntimeError("Excel export control not found on Service Types page.")

    previous = _snapshot_files(download_dir)
    _emit("ServiceType→Rate: exporting Excel.", progress)
    click_js(driver, control)
    downloaded = _wait_for_new_file(download_dir, previous, timeout=90)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = downloaded.suffix.lower() or ".xlsx"
    target = download_dir / f"service_types_export_{timestamp}{suffix}"
    downloaded.rename(target)
    _emit(f"ServiceType→Rate: download complete: {target}", progress)
    return target


def _table_header_index_map(table) -> Dict[str, int]:
    headers = table.find_elements(By.XPATH, ".//thead//th|.//thead//td")
    if not headers:
        headers = table.find_elements(By.XPATH, ".//th")
    if not headers:
        headers = table.find_elements(By.XPATH, ".//tr[1]/th|.//tr[1]/td")

    mapping: Dict[str, int] = {}
    for idx, header in enumerate(headers):
        token = _normalize_token(header.text)
        if token == "servicetype" and "Service Type" not in mapping:
            mapping["Service Type"] = idx
        elif token == "id" and "ID" not in mapping:
            mapping["ID"] = idx
        elif token == "package" and "Package" not in mapping:
            mapping["Package"] = idx
        elif token == "servicecode" and "Service Code" not in mapping:
            mapping["Service Code"] = idx
        elif token == "payrollcode" and "Payroll Code" not in mapping:
            mapping["Payroll Code"] = idx
        elif token == "billingtype" and "Billing Type" not in mapping:
            mapping["Billing Type"] = idx
        elif token == "paymenttype" and "Payment Type" not in mapping:
            mapping["Payment Type"] = idx
        elif token == "taxrate" and "Tax Rate" not in mapping:
            mapping["Tax Rate"] = idx
        elif token in {"defrate", "defaultrate", "rate"} and "Def. Rate" not in mapping:
            mapping["Def. Rate"] = idx
        elif (token.startswith("recur") or token == "recurring") and "Recur." not in mapping:
            mapping["Recur."] = idx
        elif token in {"rulestag", "ruletag", "rules"} and "Rules Tag" not in mapping:
            mapping["Rules Tag"] = idx
        elif token == "deleted" and "Deleted" not in mapping:
            mapping["Deleted"] = idx

    return mapping


def _extract_rows_from_html_table(table, on_row: RowCallback = None) -> List[Dict[str, str]]:
    mapping = _table_header_index_map(table)
    if "Service Type" not in mapping or "ID" not in mapping:
        raise RuntimeError(
            "HTML fallback table missing required headers 'Service Type' and/or 'ID'."
        )
    rows = table.find_elements(
        By.XPATH,
        ".//tr[td and .//a[contains(@href,'service-type-details.asp') and contains(@href,'eid=')]]",
    )
    if not rows:
        rows = table.find_elements(By.XPATH, ".//tbody/tr[td]")
    if not rows:
        rows = table.find_elements(By.XPATH, ".//tr[td]")

    extracted: List[Dict[str, str]] = []
    for row in rows:
        cells = row.find_elements(By.XPATH, "./td")
        if not cells:
            continue

        def read(column: str) -> str:
            idx = mapping.get(column)
            if idx is None or idx >= len(cells):
                return ""
            return _normalize_text(cells[idx].text)

        service_type = read("Service Type")
        if not service_type:
            continue

        link = ""
        idx = mapping.get("Service Type")
        if idx is not None and idx < len(cells):
            try:
                anchor = cells[idx].find_element(
                    By.XPATH,
                    ".//a[contains(@href,'service-type-details.asp') and contains(@href,'eid=')]",
                )
                link = _normalize_text(anchor.get_attribute("href") or "")
            except Exception:
                link = ""

        parsed = _canonical_row(
            {
                "Service Type": service_type,
                "ID": read("ID"),
                "Package": read("Package"),
                "Service Code": read("Service Code"),
                "Payroll Code": read("Payroll Code"),
                "Billing Type": read("Billing Type"),
                "Payment Type": read("Payment Type"),
                "Tax Rate": read("Tax Rate"),
                "Def. Rate": read("Def. Rate"),
                "Recur.": read("Recur."),
                "Rules Tag": read("Rules Tag"),
                "Deleted": read("Deleted"),
                "ServiceTypeLink": link,
            }
        )
        extracted.append(parsed)
        if on_row:
            try:
                on_row(parsed)
            except Exception:
                pass
    return extracted


def _parse_export_file(path: Path, on_row: RowCallback = None) -> List[Dict[str, str]]:
    try:
        import pandas as pd
    except Exception as exc:
        raise RuntimeError("pandas is required for parsing Service Types export files.") from exc

    if path.suffix.lower() in {".csv", ".txt"}:
        df = pd.read_csv(path)
    else:
        df = pd.read_excel(path)

    header_tokens = {_normalize_token(col) for col in df.columns}
    if "servicetype" not in header_tokens or "id" not in header_tokens:
        raise RuntimeError(
            "Export file missing required headers 'Service Type' and/or 'ID'."
        )

    rows: List[Dict[str, str]] = []
    for record in df.fillna("").to_dict(orient="records"):
        normalized = normalize_external_row(record)
        if not normalized["Service Type"]:
            continue
        rows.append(normalized)
        if on_row:
            try:
                on_row(normalized)
            except Exception:
                pass
    return rows


def _verify_dataset(rows: List[Dict[str, str]]):
    if not rows:
        raise RuntimeError("No Service Types rows captured.")

    # Required schema keys present
    for idx, row in enumerate(rows, start=1):
        for column in DATASET_COLUMNS:
            if column not in row:
                raise RuntimeError(f"Missing required column '{column}' in row {idx}.")

    # Numeric-ish ID verification
    ids = [row.get("ID", "") for row in rows if _normalize_text(row.get("ID", ""))]
    numeric_ids = [value for value in ids if _intish(value) is not None]
    if ids and len(numeric_ids) / len(ids) < 0.8:
        raise RuntimeError(
            f"ID verification failed: only {len(numeric_ids)}/{len(ids)} IDs are numeric-ish."
        )

    # Link verification for rows with IDs
    with_id = [row for row in rows if _normalize_text(row.get("ID", ""))]
    with_link = [row for row in with_id if _normalize_text(row.get("ServiceTypeLink", ""))]
    if with_id and len(with_link) != len(with_id):
        raise RuntimeError(
            f"ServiceTypeLink verification failed: {len(with_link)}/{len(with_id)} rows with ID have links."
        )


def _write_csv(rows: List[Dict[str, str]], path: Path):
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=DATASET_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in DATASET_COLUMNS})


def _write_xlsx(rows: List[Dict[str, str]], path: Path):
    try:
        from openpyxl import Workbook
    except Exception as exc:
        raise RuntimeError("openpyxl is required to write XLSX output.") from exc

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "ServiceTypes"
    sheet.append(DATASET_COLUMNS)
    for row in rows:
        sheet.append([row.get(column, "") for column in DATASET_COLUMNS])
    workbook.save(path)


def _save_outputs(rows: List[Dict[str, str]], output_root: Path, progress: ProgressCallback = None):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = output_root / f"ServiceTypes_{timestamp}.csv"
    xlsx_path = output_root / f"ServiceTypes_{timestamp}.xlsx"
    latest_csv = output_root / "ServiceTypes_latest.csv"
    latest_xlsx = output_root / "ServiceTypes_latest.xlsx"

    _write_csv(rows, csv_path)
    shutil.copy2(csv_path, latest_csv)
    _emit(f"ServiceType→Rate: wrote CSV: {csv_path}", progress)

    _write_xlsx(rows, xlsx_path)
    shutil.copy2(xlsx_path, latest_xlsx)
    _emit(f"ServiceType→Rate: wrote XLSX: {xlsx_path}", progress)

    return {
        "csv_path": csv_path,
        "xlsx_path": xlsx_path,
        "latest_csv": latest_csv,
        "latest_xlsx": latest_xlsx,
    }


def _diagnostics(driver, output_root: Path) -> Dict[str, str]:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_path = output_root / f"ServiceTypes_error_{timestamp}.png"
    html_path = output_root / f"ServiceTypes_error_{timestamp}.html"

    current_url = _normalize_text(getattr(driver, "current_url", ""))

    html_len = 0
    try:
        driver.save_screenshot(str(screenshot_path))
    except Exception:
        screenshot_path = Path("")

    try:
        html = driver.page_source or ""
        html_len = len(html)
        html_path.write_text(html, encoding="utf-8")
    except Exception:
        html_path = Path("")

    return {
        "current_url": current_url,
        "screenshot": str(screenshot_path) if screenshot_path else "",
        "html_path": str(html_path) if html_path else "",
        "html_length": str(html_len),
    }


def capture_service_type_rates(
    *,
    headless: bool = True,
    on_row: RowCallback = None,
    on_progress: ProgressCallback = None,
) -> Dict[str, object]:
    """
    Capture global Service Types data from service-types.asp (read-only).

    Notes for developers:
    - Add Appointment is unreliable for global service types because it can be
      context-specific and dynamically rendered differently.
    - service-types.asp is the stable system-level source with export support.
    """
    ensure_credentials()
    output_root, download_dir = _ensure_paths()
    started_at = datetime.now(timezone.utc).isoformat()

    driver = build_chrome_driver(headless=headless, download_dir=download_dir)
    method = ""
    rows: List[Dict[str, str]] = []

    _emit("[ServiceType→Rate] Capture started.", on_progress)
    _emit(
        "ServiceType→Rate: using Service Types index as authoritative source (Add Appointment dropdown is not stable).",
        on_progress,
    )

    try:
        _emit("ServiceType→Rate: logging into TurnPoint.", on_progress)
        try:
            login(driver)
        except Exception as login_exc:
            hint = _login_failure_hint(driver)
            if hint:
                raise RuntimeError(f"TurnPoint login failed: {hint}") from login_exc
            raise RuntimeError("TurnPoint login failed before reaching dashboard.") from login_exc
        if _is_password_change_required(driver):
            _emit(
                "ServiceType→Rate warning: password-change notice detected on dashboard; continuing unless it blocks navigation.",
                on_progress,
            )
        if _is_password_change_blocking(driver):
            raise RuntimeError(
                "Password change page is blocking automation. Update TurnPoint password and retry."
            )
        if not _is_authenticated(driver):
            hint = _login_failure_hint(driver)
            if hint:
                raise RuntimeError(f"TurnPoint authentication check failed after login: {hint}")
            raise RuntimeError("TurnPoint authentication check failed after login.")

        _navigate_to_service_types_page(driver, on_progress)
        if _is_password_change_blocking(driver):
            raise RuntimeError(
                "TurnPoint redirected to a password change page while opening Service Types."
            )

        selected_size = _set_records_per_page(driver, on_progress)
        _set_deleted_filter_no(driver, on_progress)
        _refresh_search_results(driver, on_progress)

        table = _wait_for_table_rows(driver, timeout=20)
        if table is None:
            raise RuntimeError("Service Types table not found after refresh.")

        try:
            export_path = _download_export_file(driver, download_dir, on_progress)
            _emit("ServiceType→Rate: parsing workbook.", on_progress)
            rows = _parse_export_file(export_path, on_row=on_row)
            method = "export"
        except Exception as export_exc:
            _emit(
                f"ServiceType→Rate: export failed ({export_exc}), falling back to HTML parse.",
                on_progress,
            )
            _navigate_to_service_types_page(driver, on_progress)
            _set_records_per_page(driver, on_progress)
            _set_deleted_filter_no(driver, on_progress)
            _refresh_search_results(driver, on_progress)
            table = _wait_for_table_rows(driver, timeout=20)
            rows = _extract_rows_from_html_table(table, on_row=on_row)
            method = "html"

        _verify_dataset(rows)
        _emit(f"ServiceType→Rate: capture complete: {len(rows)} rows.", on_progress)

        saved = _save_outputs(rows, output_root, on_progress)
        return {
            "rows": rows,
            "row_count": len(rows),
            "method": method,
            "selected_page_size": selected_size,
            "started_at": started_at,
            "output_root": output_root,
            **saved,
        }
    except Exception as exc:
        diag = _diagnostics(driver, output_root)
        message = (
            f"ServiceType→Rate capture failed: {exc} | "
            f"current_url={diag.get('current_url','')} | "
            f"screenshot={diag.get('screenshot','')} | "
            f"html_length={diag.get('html_length','0')}"
        )
        _emit(message, on_progress)
        raise RuntimeError(message) from exc
    finally:
        driver.quit()

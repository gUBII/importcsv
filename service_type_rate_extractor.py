import csv
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By

from importcsv import ARCHIVE_ROOT, build_chrome_driver, ensure_credentials, login, log_message
from selenium_helpers import click_js, wait_for

SERVICE_TYPES_PAGE_URL = "https://tp1.com.au/service-types.asp?posted=yes&fld586=False"
SERVICE_TYPE_DETAILS_URL_TMPL = "https://tp1.com.au/service-type-details.asp?eid={eid}"

DATASET_COLUMNS = [
    "ServiceType",
    "ServiceTypeLink",
    "ServiceTypeID",
    "Package",
    "ServiceCode",
    "PayrollCode",
    "BillingType",
    "PaymentType",
    "TaxRate",
    "DefaultRate",
    "Recurring",
    "RulesTag",
    "Deleted",
    "CapturedAt",
    "SourcePage",
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


def _canonical_row(values: Dict[str, str], captured_at: str) -> Dict[str, str]:
    row = {column: "" for column in DATASET_COLUMNS}
    row.update({k: _normalize_text(v) for k, v in values.items()})

    row["ServiceTypeID"] = row["ServiceTypeID"] or _extract_id_from_link(row["ServiceTypeLink"])
    if not row["ServiceTypeLink"] and row["ServiceTypeID"]:
        row["ServiceTypeLink"] = SERVICE_TYPE_DETAILS_URL_TMPL.format(eid=row["ServiceTypeID"])

    row["Deleted"] = _normalize_deleted(row.get("Deleted", ""))
    row["CapturedAt"] = captured_at
    row["SourcePage"] = SERVICE_TYPES_PAGE_URL

    return {column: _normalize_text(row.get(column, "")) for column in DATASET_COLUMNS}


def normalize_external_row(raw_row: Dict[str, str], captured_at: Optional[str] = None) -> Dict[str, str]:
    """Map arbitrary input headers to canonical dataset columns."""
    captured_at = captured_at or datetime.now(timezone.utc).isoformat()
    token_map = {_normalize_token(key): _normalize_text(value) for key, value in (raw_row or {}).items()}

    def pick(*aliases: str) -> str:
        for alias in aliases:
            token = _normalize_token(alias)
            if token in token_map and token_map[token] != "":
                return token_map[token]
        return ""

    mapped = {
        "ServiceType": pick("ServiceType", "Service Type", "Type", "Name"),
        "ServiceTypeLink": pick("ServiceTypeLink", "Service Type Link", "Link", "URL", "Details URL"),
        "ServiceTypeID": pick("ServiceTypeID", "Service Type ID", "ID", "EID"),
        "Package": pick("Package"),
        "ServiceCode": pick("ServiceCode", "Service Code", "Code"),
        "PayrollCode": pick("PayrollCode", "Payroll Code"),
        "BillingType": pick("BillingType", "Billing Type", "Billing"),
        "PaymentType": pick("PaymentType", "Payment Type", "Payment"),
        "TaxRate": pick("TaxRate", "Tax Rate", "Tax", "GST"),
        "DefaultRate": pick("DefaultRate", "Default Rate", "Def Rate", "Def. Rate", "Rate"),
        "Recurring": pick("Recurring", "Recur", "Recur.", "Recurrence"),
        "RulesTag": pick("RulesTag", "Rules Tag", "Rules", "Tag"),
        "Deleted": pick("Deleted"),
    }

    return _canonical_row(mapped, captured_at)


def _ensure_paths() -> tuple[Path, Path]:
    base = (ARCHIVE_ROOT / "ServiceTypeRateExtractor").resolve()
    downloads = base / "_downloads"
    base.mkdir(parents=True, exist_ok=True)
    downloads.mkdir(parents=True, exist_ok=True)
    return base, downloads


def _snapshot_files(folder: Path) -> set[str]:
    folder.mkdir(parents=True, exist_ok=True)
    return {p.name for p in folder.iterdir() if p.is_file()}


def _wait_for_new_file(folder: Path, previous: set[str], timeout: int = 60) -> Path:
    deadline = time.time() + timeout
    while time.time() < deadline:
        ready = [
            p
            for p in folder.iterdir()
            if p.is_file() and not p.name.endswith(".crdownload") and p.name not in previous
        ]
        if ready:
            ready.sort(key=lambda item: item.stat().st_mtime, reverse=True)
            return ready[0]
        time.sleep(0.4)
    raise TimeoutException("Timed out waiting for Service Types export download.")


def _is_authenticated(driver) -> bool:
    if "login" in _normalize_text(driver.current_url).lower():
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


def _navigate_to_service_types_page(driver, progress: ProgressCallback = None):
    _emit("ServiceType→Rate: navigating to Service Types page.", progress)
    driver.get(SERVICE_TYPES_PAGE_URL)
    wait_for(driver, By.TAG_NAME, "body", timeout=20)


def _find_service_types_table(driver):
    tables = driver.find_elements(By.XPATH, "//table")
    for table in tables:
        try:
            if not table.is_displayed():
                continue
        except Exception:
            continue
        headers = table.find_elements(By.XPATH, ".//thead//th|.//thead//td")
        if not headers:
            headers = table.find_elements(By.XPATH, ".//tr[1]/th|.//tr[1]/td")
        header_tokens = [_normalize_token(h.text) for h in headers]
        has_service_type = any("servicetype" in token for token in header_tokens)
        has_id = any(token == "id" or token.endswith("id") for token in header_tokens)
        if has_service_type and has_id:
            return table
    return None


def _table_header_index_map(table) -> Dict[str, int]:
    headers = table.find_elements(By.XPATH, ".//thead//th|.//thead//td")
    if not headers:
        headers = table.find_elements(By.XPATH, ".//tr[1]/th|.//tr[1]/td")

    mapping: Dict[str, int] = {}
    for idx, header in enumerate(headers):
        token = _normalize_token(header.text)
        if "servicetype" in token and "ServiceType" not in mapping:
            mapping["ServiceType"] = idx
        elif token == "id" and "ServiceTypeID" not in mapping:
            mapping["ServiceTypeID"] = idx
        elif "package" in token and "Package" not in mapping:
            mapping["Package"] = idx
        elif "servicecode" in token and "ServiceCode" not in mapping:
            mapping["ServiceCode"] = idx
        elif "payrollcode" in token and "PayrollCode" not in mapping:
            mapping["PayrollCode"] = idx
        elif "billingtype" in token and "BillingType" not in mapping:
            mapping["BillingType"] = idx
        elif "paymenttype" in token and "PaymentType" not in mapping:
            mapping["PaymentType"] = idx
        elif "tax" in token and "TaxRate" not in mapping:
            mapping["TaxRate"] = idx
        elif token in {"defrate", "defaultrate", "rate"} and "DefaultRate" not in mapping:
            mapping["DefaultRate"] = idx
        elif "recur" in token and "Recurring" not in mapping:
            mapping["Recurring"] = idx
        elif "rule" in token and "RulesTag" not in mapping:
            mapping["RulesTag"] = idx
        elif "deleted" in token and "Deleted" not in mapping:
            mapping["Deleted"] = idx

    return mapping


def _extract_rows_from_html_table(table, captured_at: str, on_row: RowCallback = None):
    mapping = _table_header_index_map(table)
    rows = table.find_elements(By.XPATH, ".//tbody/tr")
    if not rows:
        rows = table.find_elements(By.XPATH, ".//tr[position()>1]")

    extracted: List[Dict[str, str]] = []
    for row in rows:
        cells = row.find_elements(By.XPATH, "./td")
        if not cells:
            continue

        def read(col_name: str) -> str:
            idx = mapping.get(col_name)
            if idx is None or idx >= len(cells):
                return ""
            return _normalize_text(cells[idx].text)

        service_type = read("ServiceType")
        if not service_type and cells:
            service_type = _normalize_text(cells[0].text)
        if not service_type:
            continue

        details_link = ""
        if "ServiceType" in mapping:
            idx = mapping["ServiceType"]
            if idx < len(cells):
                try:
                    anchor = cells[idx].find_element(
                        By.XPATH,
                        ".//a[contains(@href,'service-type-details.asp') and contains(@href,'eid=')]",
                    )
                    details_link = _normalize_text(anchor.get_attribute("href") or "")
                except Exception:
                    details_link = ""

        data = {
            "ServiceType": service_type,
            "ServiceTypeLink": details_link,
            "ServiceTypeID": read("ServiceTypeID"),
            "Package": read("Package"),
            "ServiceCode": read("ServiceCode"),
            "PayrollCode": read("PayrollCode"),
            "BillingType": read("BillingType"),
            "PaymentType": read("PaymentType"),
            "TaxRate": read("TaxRate"),
            "DefaultRate": read("DefaultRate"),
            "Recurring": read("Recurring"),
            "RulesTag": read("RulesTag"),
            "Deleted": read("Deleted"),
        }
        normalized = _canonical_row(data, captured_at)
        extracted.append(normalized)
        if on_row:
            try:
                on_row(normalized)
            except Exception:
                pass

    return extracted


def _find_export_control(driver):
    selectors = [
        "//a[contains(@onclick,'generateXL')]",
        "//a[contains(translate(.,'EXCEL','excel'),'excel')]",
        "//button[contains(translate(.,'EXCEL','excel'),'excel')]",
        "//img[contains(translate(@alt,'EXCEL','excel'),'excel')]/parent::a",
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


def _download_export_xlsx(driver, download_dir: Path, progress: ProgressCallback = None) -> Path:
    export_control = _find_export_control(driver)
    if export_control is None:
        raise RuntimeError("Service Types export control not found.")

    previous = _snapshot_files(download_dir)
    _emit("ServiceType→Rate: export detected, downloading XLSX...", progress)
    click_js(driver, export_control)
    downloaded = _wait_for_new_file(download_dir, previous, timeout=75)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = downloaded.suffix or ".xlsx"
    target = download_dir / f"service_types_export_{timestamp}{suffix}"
    downloaded.rename(target)
    _emit(f"ServiceType→Rate: download completed: {target}", progress)
    return target


def _parse_export_file(path: Path, captured_at: str, on_row: RowCallback = None):
    try:
        import pandas as pd
    except Exception as exc:
        raise RuntimeError("pandas is required to parse Service Types export files.") from exc

    if path.suffix.lower() in {".csv", ".txt"}:
        df = pd.read_csv(path)
    else:
        df = pd.read_excel(path)

    rows: List[Dict[str, str]] = []
    for record in df.fillna("").to_dict(orient="records"):
        normalized = normalize_external_row(record, captured_at=captured_at)
        if not normalized["ServiceType"]:
            continue
        rows.append(normalized)
        if on_row:
            try:
                on_row(normalized)
            except Exception:
                pass
    return rows


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
    _emit(f"ServiceType→Rate: saved CSV: {csv_path}", progress)

    _write_xlsx(rows, xlsx_path)
    shutil.copy2(xlsx_path, latest_xlsx)
    _emit(f"ServiceType→Rate: saved XLSX: {xlsx_path}", progress)

    return {
        "csv_path": csv_path,
        "xlsx_path": xlsx_path,
        "latest_csv": latest_csv,
        "latest_xlsx": latest_xlsx,
    }


def _diagnostics(driver, output_root: Path) -> Dict[str, str]:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot = output_root / f"ServiceTypes_error_{timestamp}.png"
    html_path = output_root / f"ServiceTypes_error_{timestamp}.html"
    current_url = ""
    html_len = 0
    try:
        current_url = _normalize_text(driver.current_url)
    except Exception:
        current_url = ""

    try:
        driver.save_screenshot(str(screenshot))
    except Exception:
        screenshot = Path("")

    try:
        html = driver.page_source or ""
        html_len = len(html)
        html_path.write_text(html, encoding="utf-8")
    except Exception:
        html_path = Path("")

    return {
        "current_url": current_url,
        "screenshot": str(screenshot) if screenshot else "",
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
    Capture global Service Types data from TurnPoint (read-only, non-purge).

    Strategy:
    1) Login and open Service Types page.
    2) Preferred: trigger built-in export and parse file.
    3) Fallback: scrape HTML table.
    4) Save canonical CSV + XLSX and latest copies.
    """
    ensure_credentials()
    output_root, download_dir = _ensure_paths()
    captured_at = datetime.now(timezone.utc).isoformat()

    driver = build_chrome_driver(headless=headless, download_dir=download_dir)
    rows: List[Dict[str, str]] = []
    method = ""

    _emit("[ServiceType→Rate] Capture started.", on_progress)
    _emit(
        "ServiceType→Rate: using Service Types index as authoritative source (Add Appointment dropdown is not stable).",
        on_progress,
    )
    try:
        _emit("ServiceType→Rate: logging into TurnPoint.", on_progress)
        login(driver)
        if not _is_authenticated(driver):
            raise RuntimeError("TurnPoint authentication check failed after login.")

        _navigate_to_service_types_page(driver, on_progress)

        table = _find_service_types_table(driver)
        if table is None:
            _emit(
                "ServiceType→Rate warning: Service Types table header not detected immediately; trying export first.",
                on_progress,
            )

        export_path = None
        try:
            export_path = _download_export_xlsx(driver, download_dir, on_progress)
            rows = _parse_export_file(export_path, captured_at, on_row=on_row)
            method = "export"
        except Exception as export_exc:
            _emit(
                f"ServiceType→Rate warning: export path failed ({export_exc}). Falling back to HTML table scrape.",
                on_progress,
            )
            _navigate_to_service_types_page(driver, on_progress)
            table = _find_service_types_table(driver)
            if table is None:
                raise RuntimeError("Service Types table was not found for fallback scraping.") from export_exc
            rows = _extract_rows_from_html_table(table, captured_at, on_row=on_row)
            method = "html"

        if not rows:
            raise RuntimeError("No Service Types rows were captured from export or HTML fallback.")

        _emit(f"ServiceType→Rate: parsed {len(rows)} rows.", on_progress)
        saved = _save_outputs(rows, output_root, on_progress)
        _emit("ServiceType→Rate: capture complete.", on_progress)

        return {
            "rows": rows,
            "row_count": len(rows),
            "method": method,
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

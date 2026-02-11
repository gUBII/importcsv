import csv
import json
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs

from selenium.common.exceptions import TimeoutException
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
from selenium_helpers import wait_for
from service_type_rate_extractor import DATASET_COLUMNS

ProgressCallback = Optional[Callable[[str], None]]
EventCallback = Optional[Callable[[Dict[str, str]], None]]

DISCOVERY_COLUMNS = [
    "Parent Service Type",
    "Service Variant Label",
    "Service Type ID",
    "Item Number",
    "Service Code",
    "Rate",
    "Rate Source",
    "Unit Type",
    "Setter Value",
    "Payload JSON",
    "Source Client ID",
    "Captured At (UTC)",
]

ENRICHED_COLUMNS = DATASET_COLUMNS + [
    "Item Number",
    "Rate",
    "Rate Source",
    "Discovery Parent Service Type",
    "Discovery Service Variant",
    "Discovery Setter Value",
    "Discovery Source Client ID",
    "Discovery Captured At (UTC)",
    "Discovery Payload JSON",
]

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

REFERENCE_LATEST_CSV = (
    ARCHIVE_ROOT / "ServiceTypeRateExtractor" / "ServiceTypes_latest.csv"
).resolve()
DISCOVERY_LATEST_CSV = (
    ARCHIVE_ROOT / "ServiceTypeRateExtractor" / "AppointmentItemDiscovery_latest.csv"
).resolve()

CHECKER_CLIENT = "CHK_CLIENT_PAGE_OPEN"
CHECKER_APPOINTMENT = "CHK_APPOINTMENT_ENTRY_REACHED"
CHECKER_DROPDOWN_PRESENT = "CHK_DROPDOWN_PRESENT"
CHECKER_DROPDOWN_STATE = "CHK_DROPDOWN_VISIBLE_ENABLED"
CHECKER_DROPDOWN_OPTIONS = "CHK_DROPDOWN_OPTIONS_EXTRACTED"
CHECKER_DROPDOWN_NONEMPTY = "CHK_DROPDOWN_NONEMPTY"
CHECKER_DROPDOWN_EMPTY = "CHK_DROPDOWN_EMPTY"
CHECKER_ROUTE_ASSIST = "CHK_ROUTE_ASSIST_REACHED"
CHECKER_APPOINTMENT_NEW = "CHK_APPOINTMENT_NEW_REACHED"
CHECKER_CLIENT_COMBO_PRESENT = "CHK_CLIENT_COMBO_PRESENT"
CHECKER_CLIENT_OPTIONS_NONEMPTY = "CHK_CLIENT_OPTIONS_NONEMPTY"
CHECKER_SERVICE_COMBO_PRESENT = "CHK_SERVICE_COMBO_PRESENT"
CHECKER_SERVICE_OPTIONS_NONEMPTY = "CHK_SERVICE_OPTIONS_NONEMPTY"
CHECKER_DETAILS_PAGE_OPEN = "CHK_DETAILS_PAGE_OPEN"
CHECKER_ITEM_NUMBER_RESOLVED = "CHK_ITEM_NUMBER_RESOLVED"

ASSIST_APPOINTMENTS_ALL_URL = "https://tp1.com.au/appointments-all.asp"
ASSIST_APPOINTMENTS_NEW_URL = "https://assist.turnpoint.co/appointments/new"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _safe_json(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return "{}"


def _ensure_output_paths(run_id: str) -> Tuple[Path, Path]:
    output_root = (ARCHIVE_ROOT / "ServiceTypeRateExtractor").resolve()
    diagnostics_root = output_root / "diagnostics" / run_id
    output_root.mkdir(parents=True, exist_ok=True)
    diagnostics_root.mkdir(parents=True, exist_ok=True)
    return output_root, diagnostics_root


def _write_csv(rows: List[Dict[str, str]], fieldnames: List[str], path: Path):
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in fieldnames})


def _write_xlsx(rows: List[Dict[str, str]], fieldnames: List[str], path: Path):
    try:
        from openpyxl import Workbook
    except Exception as exc:
        raise RuntimeError("openpyxl is required to write XLSX output.") from exc

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Dataset"
    sheet.append(fieldnames)
    for row in rows:
        sheet.append([row.get(column, "") for column in fieldnames])
    workbook.save(path)


def _save_dataset_outputs(
    rows: List[Dict[str, str]],
    fieldnames: List[str],
    output_root: Path,
    *,
    prefix: str,
    latest_name: str,
    progress: ProgressCallback = None,
) -> Dict[str, Path]:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = output_root / f"{prefix}_{timestamp}.csv"
    xlsx_path = output_root / f"{prefix}_{timestamp}.xlsx"
    latest_csv = output_root / latest_name
    latest_xlsx = output_root / latest_name.replace(".csv", ".xlsx")

    _write_csv(rows, fieldnames, csv_path)
    shutil.copy2(csv_path, latest_csv)
    _write_xlsx(rows, fieldnames, xlsx_path)
    shutil.copy2(xlsx_path, latest_xlsx)

    _emit(f"[ItemDiscovery] wrote CSV: {csv_path}", progress)
    _emit(f"[ItemDiscovery] wrote XLSX: {xlsx_path}", progress)
    return {
        "csv_path": csv_path,
        "xlsx_path": xlsx_path,
        "latest_csv": latest_csv,
        "latest_xlsx": latest_xlsx,
    }


def _load_csv_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {path}")
    with path.open("r", newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        rows: List[Dict[str, str]] = []
        for raw in reader:
            normalized: Dict[str, str] = {}
            for key, value in (raw or {}).items():
                normalized[_normalize_text(key)] = _normalize_text(value)
            if any(normalized.values()):
                rows.append(normalized)
        return rows


def _parse_probe_clients(probe_client_id: Optional[str]) -> List[Optional[str]]:
    if probe_client_id is None:
        return [None]
    parts = [chunk.strip() for chunk in str(probe_client_id).split(",")]
    values = [chunk for chunk in parts if chunk]
    return values or [None]


def _is_logged_in(driver) -> bool:
    url = _normalize_text(getattr(driver, "current_url", "")).lower()
    return "login" not in url


def _body_text(driver) -> str:
    try:
        return _normalize_text(driver.find_element(By.TAG_NAME, "body").text)
    except Exception:
        return ""


def _is_404_page(driver) -> bool:
    body = _body_text(driver).lower()
    title = _normalize_text(getattr(driver, "title", "")).lower()
    if "404" in title and "not found" in title:
        return True
    return ("http error 404" in body) or ("0x80070002" in body)


def _is_auth_session_page(driver) -> bool:
    url = _normalize_text(getattr(driver, "current_url", "")).lower()
    body = _body_text(driver).lower()
    return "assist.turnpoint.co/auth-session" in url or "authenticating" in body


def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 30.0,
    interval: float = 0.5,
) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _client_url_candidates(client_id: str) -> List[str]:
    base = BASE_URL.rstrip("/")
    return [
        f"{base}/client-details.asp?eid={client_id}",
        f"{base}/client-details.asp?eid={client_id}&posted=yes",
    ]


def _appointment_url_candidates(client_id: Optional[str]) -> List[str]:
    base = BASE_URL.rstrip("/")
    if client_id:
        return [
            f"{base}/appointments.asp?eid={client_id}&action=add",
            f"{base}/appointment-details.asp?eid={client_id}&new=yes",
            f"{base}/appointments.asp?posted=yes&eid={client_id}",
            f"{base}/client-details.asp?eid={client_id}&BREAKDOWN_SHOW_APPOINTMENTS=yes",
        ]
    return [
        f"{base}/appointments.asp?posted=yes",
        f"{base}/appointment-details.asp?new=yes",
        f"{base}/appointments.asp?action=add",
    ]


def _open_assist_appointments_new(driver, recorder: "DiagnosticsRecorder") -> bool:
    """
    Attempt Assist flow:
      tp1 appointments-all -> assist appointments -> assist /appointments/new
    """
    reached_assist = False
    for _attempt in range(3):
        if not _navigate_and_wait_body(driver, ASSIST_APPOINTMENTS_ALL_URL, timeout=30):
            continue

        # Wait for either appointments, auth-session, or login page on Assist.
        _wait_until(
            lambda: "assist.turnpoint.co/" in _normalize_text(getattr(driver, "current_url", "")).lower(),
            timeout=25,
        )
        current_url = _normalize_text(getattr(driver, "current_url", "")).lower()

        if "/login" in current_url:
            # Retry by re-entering through tp1 bridge URL.
            time.sleep(1.0)
            continue

        if _is_auth_session_page(driver):
            settled = _wait_until(
                lambda: (
                    "assist.turnpoint.co/appointments" in _normalize_text(getattr(driver, "current_url", "")).lower()
                    and not _is_auth_session_page(driver)
                ),
                timeout=35,
            )
            if not settled:
                continue

        if "assist.turnpoint.co/appointments" in _normalize_text(
            getattr(driver, "current_url", "")
        ).lower() and not _is_404_page(driver):
            reached_assist = True
            break

    if not reached_assist:
        return False

    current_url = _normalize_text(getattr(driver, "current_url", ""))
    recorder.emit_checker(
        step=CHECKER_ROUTE_ASSIST,
        status="pass",
        code=CHECKER_ROUTE_ASSIST,
        message=f"Assist route reached at {current_url}",
        url=current_url,
    )
    recorder.emit_event(
        level="INFO",
        step=CHECKER_ROUTE_ASSIST,
        code=CHECKER_ROUTE_ASSIST,
        message="Assist route reached.",
        url=current_url,
        context={"title": _normalize_text(getattr(driver, "title", ""))},
    )

    if not _navigate_and_wait_body(driver, ASSIST_APPOINTMENTS_NEW_URL, timeout=30):
        return False

    ready = _wait_until(
        lambda: (
            "/appointments/new" in _normalize_text(getattr(driver, "current_url", "")).lower()
            and "appointment details - new" in _body_text(driver).lower()
            and "assist.turnpoint.co" in _normalize_text(getattr(driver, "current_url", "")).lower()
            and not _is_404_page(driver)
            and "/login" not in _normalize_text(getattr(driver, "current_url", "")).lower()
        ),
        timeout=45,
    )
    if not ready:
        return False

    ready_url = _normalize_text(getattr(driver, "current_url", ""))
    recorder.emit_checker(
        step=CHECKER_APPOINTMENT_NEW,
        status="pass",
        code=CHECKER_APPOINTMENT_NEW,
        message=f"Assist appointment form reached at {ready_url}",
        url=ready_url,
    )
    recorder.emit_event(
        level="INFO",
        step=CHECKER_APPOINTMENT_NEW,
        code=CHECKER_APPOINTMENT_NEW,
        message="Assist new-appointment page is ready.",
        url=ready_url,
        context={"title": _normalize_text(getattr(driver, "title", ""))},
    )
    return True


def _find_assist_react_input(driver, container_id: str):
    container_xpath = f"//*[@id='{container_id}']"
    input_xpath = (
        f"{container_xpath}//input[starts-with(@id,'react-select-') and contains(@id,'-input')]"
    )
    try:
        container = driver.find_element(By.XPATH, container_xpath)
        react_input = container.find_element(By.XPATH, ".//input[starts-with(@id,'react-select-') and contains(@id,'-input')]")
        hidden = container.find_element(By.XPATH, f".//input[@type='hidden' and @name='{container_id}']")
        return container, react_input, hidden, input_xpath
    except Exception:
        return None, None, None, input_xpath


def _normalize_assist_options(raw_options: Any) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if not isinstance(raw_options, list):
        return out
    for option in raw_options:
        if not isinstance(option, dict):
            continue
        nested = option.get("options")
        if isinstance(nested, list):
            out.extend(_normalize_assist_options(nested))
            continue
        label = _normalize_text(option.get("label", ""))
        value = _normalize_text(option.get("value", ""))
        if not label and not value:
            continue
        out.append({"label": label, "value": value})
    deduped: List[Dict[str, str]] = []
    seen: set[str] = set()
    for option in out:
        key = f"{_normalize_text(option.get('label', '')).lower()}|{_normalize_text(option.get('value', ''))}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(option)
    return deduped


def _extract_assist_options_from_dom(driver) -> List[Dict[str, str]]:
    options: List[Dict[str, str]] = []
    selectors = [
        "//div[@role='option' and starts-with(@id,'react-select-')]",
        "//div[contains(@class,'form-input-select__option')]",
    ]
    seen: set[str] = set()
    for selector in selectors:
        try:
            elements = driver.find_elements(By.XPATH, selector)
        except Exception:
            elements = []
        for elem in elements:
            try:
                label = _normalize_text(elem.text or "")
            except Exception:
                label = ""
            if not label:
                continue
            try:
                value = _normalize_text(elem.get_attribute("data-value") or "")
            except Exception:
                value = ""
            key = f"{label.lower()}|{value}"
            if key in seen:
                continue
            seen.add(key)
            options.append({"label": label, "value": value})
    return options


def _assist_no_options_notice_present(driver) -> bool:
    try:
        notices = driver.find_elements(
            By.XPATH,
            "//div[contains(@class,'form-input-select__menu-notice') and contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'no options')]",
        )
    except Exception:
        return False
    return bool(notices)


def _focus_and_seed_assist_input(react_input, query: str):
    try:
        react_input.click()
    except Exception:
        pass
    try:
        react_input.send_keys(Keys.CONTROL, "a")
        react_input.send_keys(Keys.BACKSPACE)
    except Exception:
        try:
            react_input.clear()
        except Exception:
            pass
    if query:
        try:
            react_input.send_keys(query)
        except Exception:
            pass
    else:
        try:
            react_input.send_keys(Keys.ARROW_DOWN)
        except Exception:
            pass


def _candidate_queries(seed_query: str, query_candidates: Optional[List[str]]) -> List[str]:
    candidates: List[str] = []
    for raw in (query_candidates or [seed_query]):
        value = _normalize_text(raw)
        if raw == "":
            value = ""
        if value in candidates:
            continue
        candidates.append(value)
    if not candidates:
        candidates = [""]
    return candidates


def _poll_assist_options(
    driver,
    container,
    react_input,
    *,
    timeout_s: float = 8.0,
    interval_s: float = 0.4,
) -> List[Dict[str, str]]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        dom_options = _extract_assist_options_from_dom(driver)
        notice_no_options = _assist_no_options_notice_present(driver)
        react_options = _extract_assist_options_from_react(driver, container, react_input)
        if dom_options:
            if any(_normalize_text(item.get("value", "")) for item in dom_options):
                return dom_options
            if react_options:
                return react_options
            return dom_options
        if notice_no_options:
            return []
        if react_options:
            return react_options
        time.sleep(interval_s)
    return []


def _extract_assist_options_from_react(driver, container, react_input=None) -> List[Dict[str, str]]:
    """
    Read react-select options from React fiber/memoized props.
    """
    script = """
const targets = [arguments[0], arguments[1]].filter(Boolean);
let options = [];
const visitedNodes = new Set();
const visitedEls = new Set();

function optionsFromProps(props){
  if(!props) return [];
  if(Array.isArray(props.options) && props.options.length) return props.options;
  if(props.selectProps && Array.isArray(props.selectProps.options) && props.selectProps.options.length){
    return props.selectProps.options;
  }
  return [];
}

function walkFiber(node, depth){
  if(!node || depth > 45 || visitedNodes.has(node) || options.length) return;
  visitedNodes.add(node);
  const candidates = [
    node.memoizedProps || null,
    node.pendingProps || null,
    node.stateNode && node.stateNode.props ? node.stateNode.props : null,
  ];
  for(const props of candidates){
    const result = optionsFromProps(props);
    if(result.length){
      options = result;
      return;
    }
  }
  walkFiber(node.child, depth + 1);
  walkFiber(node.sibling, depth);
  walkFiber(node.return, depth + 1);
}

function walkElement(el){
  if(!el || visitedEls.has(el) || options.length) return;
  visitedEls.add(el);
  const keys = Object.getOwnPropertyNames(el || {});
  for(const key of keys){
    if(key.startsWith('__reactProps$')){
      const result = optionsFromProps(el[key]);
      if(result.length){
        options = result;
        return;
      }
    }
    if(key.startsWith('__reactFiber$')){
      walkFiber(el[key], 0);
      if(options.length) return;
    }
  }
  if(el.parentElement) walkElement(el.parentElement);
  const descendants = el.querySelectorAll ? el.querySelectorAll('*') : [];
  for(const child of descendants){
    walkElement(child);
    if(options.length) return;
  }
}

for(const el of targets){
  walkElement(el);
  if(options.length) break;
}
return options;
"""
    try:
        options = driver.execute_script(script, container, react_input)
    except Exception:
        options = []
    return _normalize_assist_options(options)


def _collect_assist_options(
    driver,
    *,
    container_id: str,
    seed_query: str,
    query_candidates: Optional[List[str]] = None,
    recorder: "DiagnosticsRecorder",
    client_id: Optional[str],
    present_checker: str,
    options_checker: str,
    discovery_debug: bool = False,
):
    container, react_input, hidden_input, selector_used = _find_assist_react_input(driver, container_id)
    if container is None or react_input is None or hidden_input is None:
        screenshot, html = recorder.capture_artifacts(driver, step=present_checker, code=present_checker)
        recorder.emit_checker(
            step=present_checker,
            status="fail",
            code=present_checker,
            message=f"Assist combobox '{container_id}' was not found.",
            client_id=_normalize_text(client_id),
            url=_normalize_text(getattr(driver, "current_url", "")),
            selector=selector_used,
            artifact_screenshot=screenshot,
            artifact_html=html,
        )
        recorder.emit_event(
            level="ERROR",
            step=present_checker,
            code=present_checker,
            message=f"Assist combobox '{container_id}' not found.",
            client_id=_normalize_text(client_id),
            url=_normalize_text(getattr(driver, "current_url", "")),
            selector=selector_used,
        )
        return None, None, []

    recorder.emit_checker(
        step=present_checker,
        status="pass",
        code=present_checker,
        message=f"Assist combobox '{container_id}' located.",
        client_id=_normalize_text(client_id),
        url=_normalize_text(getattr(driver, "current_url", "")),
        selector=selector_used,
    )

    candidates = _candidate_queries(seed_query, query_candidates)
    attempted_queries: List[str] = []
    options: List[Dict[str, str]] = []
    for attempt, query in enumerate(candidates, start=1):
        attempted_queries.append(query)
        try:
            driver.execute_script("arguments[0].click();", react_input)
        except Exception:
            pass
        _focus_and_seed_assist_input(react_input, query)
        options = _poll_assist_options(driver, container, react_input)
        if discovery_debug:
            recorder.emit_event(
                level="DEBUG",
                step=options_checker,
                code="ASSIST_COMBO_QUERY_ATTEMPT",
                message=f"Assist query attempt {attempt} for '{container_id}' observed {len(options)} option(s).",
                client_id=_normalize_text(client_id),
                url=_normalize_text(getattr(driver, "current_url", "")),
                selector=selector_used,
                option_count=len(options),
                context={
                    "attempt": attempt,
                    "query": query,
                    "container_id": container_id,
                    "page_title": _normalize_text(getattr(driver, "title", "")),
                },
            )
        if options:
            break
    status = "pass" if options else "warn"
    code = options_checker if options else CHECKER_DROPDOWN_EMPTY
    message = (
        f"Assist options extracted for '{container_id}' ({len(options)} option(s))."
        if options
        else f"Assist combobox '{container_id}' has no usable options."
    )

    artifact_screenshot = ""
    artifact_html = ""
    if not options:
        artifact_screenshot, artifact_html = recorder.capture_artifacts(
            driver, step=options_checker, code=CHECKER_DROPDOWN_EMPTY
        )

    recorder.emit_checker(
        step=options_checker,
        status=status,
        code=code,
        message=message,
        client_id=_normalize_text(client_id),
        url=_normalize_text(getattr(driver, "current_url", "")),
        selector=selector_used,
        option_count=len(options),
        artifact_screenshot=artifact_screenshot,
        artifact_html=artifact_html,
    )
    recorder.emit_event(
        level="INFO" if options else "WARN",
        step=options_checker,
        code=code,
        message=message,
        client_id=_normalize_text(client_id),
        url=_normalize_text(getattr(driver, "current_url", "")),
        selector=selector_used,
        option_count=len(options),
        context={
            "container_id": container_id,
            "seed_query": seed_query,
            "query_candidates": candidates,
            "attempted_queries": attempted_queries,
            "page_title": _normalize_text(getattr(driver, "title", "")),
            "menu_no_options_notice": _assist_no_options_notice_present(driver),
        },
    )
    if discovery_debug and options:
        for index, option in enumerate(options, start=1):
            recorder.emit_event(
                level="DEBUG",
                step=options_checker,
                code="ASSIST_OPTION_PARSED",
                message=f"Assist option #{index} parsed for '{container_id}'.",
                client_id=_normalize_text(client_id),
                url=_normalize_text(getattr(driver, "current_url", "")),
                selector=selector_used,
                option_count=index,
                context={
                    "container_id": container_id,
                    "label": _normalize_text(option.get("label", "")),
                    "value": _normalize_text(option.get("value", "")),
                    "missing_value": not bool(_normalize_text(option.get("value", ""))),
                },
            )
    return react_input, hidden_input, options


def _select_first_assist_option(driver, react_input, hidden_input) -> bool:
    option_selectors = [
        "//div[@role='option' and starts-with(@id,'react-select-')]",
        "//div[contains(@class,'form-input-select__option')]",
    ]
    for _attempt in range(3):
        for selector in option_selectors:
            try:
                options = driver.find_elements(By.XPATH, selector)
            except Exception:
                options = []
            if not options:
                continue
            first = options[0]
            try:
                driver.execute_script("arguments[0].click();", first)
            except Exception:
                try:
                    first.click()
                except Exception:
                    continue
            time.sleep(0.9)
            try:
                selected = _normalize_text(hidden_input.get_attribute("value") or "")
            except Exception:
                selected = ""
            if selected:
                return True
        try:
            react_input.click()
        except Exception:
            pass
        try:
            react_input.send_keys(Keys.ARROW_DOWN)
            react_input.send_keys(Keys.ENTER)
        except Exception:
            continue
        time.sleep(1.0)
        try:
            value = _normalize_text(hidden_input.get_attribute("value") or "")
        except Exception:
            value = ""
        if value:
            return True
    return False


def _find_service_type_dropdown(driver):
    selectors = [
        (By.XPATH, "//label[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'service type')]/following::select[1]"),
        (By.XPATH, "//select[contains(translate(@aria-label,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'service type')]"),
        (By.XPATH, "//select[contains(translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'service') and contains(translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'type')]"),
        (By.XPATH, "//select[contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'service') and contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'type')]"),
        (By.NAME, "service_type"),
        (By.NAME, "fldServiceType"),
    ]
    for by, locator in selectors:
        elements = driver.find_elements(by, locator)
        for elem in elements:
            try:
                tag = _normalize_text(elem.tag_name).lower()
                if tag != "select":
                    continue
                return elem, f"{by}:{locator}"
            except Exception:
                continue
    return None, ""


def _normalize_item_number(value: str) -> str:
    text = _normalize_text(value)
    if not text:
        return ""
    match = re.search(
        r"\b\d{2}[_-]\d{3}[_-]\d{4}[_-]\d[_-]\d(?:[_-]T)?\b",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group(0).replace("-", "_")
    candidate = text.replace("-", "_").strip().upper()
    if re.fullmatch(r"[A-Z0-9_./]{4,40}", candidate) and any(ch.isdigit() for ch in candidate):
        return candidate
    return ""


def _extract_id_token(value: str) -> str:
    text = _normalize_text(value)
    if not text:
        return ""
    match = re.search(r"\b\d{3,}\b", text)
    return match.group(0) if match else ""


def _coerce_rate_text(value: str) -> str:
    text = _normalize_text(value)
    if not text:
        return ""
    cleaned = re.sub(r"[^0-9.\-]", "", text)
    if cleaned in {"", "-", ".", "-.", ".-"}:
        return ""
    try:
        number = float(cleaned)
    except Exception:
        return text
    return f"{number:.2f}"


def _parse_option_structured_blob(value: str):
    raw = _normalize_text(value)
    if not raw:
        return {}
    if raw.startswith("{") and raw.endswith("}"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    if "=" in raw and "&" in raw:
        try:
            parsed_qs = parse_qs(raw, keep_blank_values=True)
            return {key: _normalize_text(vals[0] if vals else "") for key, vals in parsed_qs.items()}
        except Exception:
            return {}
    return {}


def _payload_get(payload: Dict[str, str], *keys: str) -> str:
    token_map = {_normalize_token(key): _normalize_text(value) for key, value in payload.items()}
    for key in keys:
        token = _normalize_token(key)
        if token in token_map and token_map[token]:
            return token_map[token]
    return ""


def _derive_parent_label(label: str) -> str:
    text = _normalize_text(label)
    if not text:
        return ""
    patterns = [
        r"\s*-\s*weekday\b.*$",
        r"\s*-\s*saturday\b.*$",
        r"\s*-\s*sunday\b.*$",
        r"\s*-\s*public holiday\b.*$",
        r"\s*-\s*night\b.*$",
        r"\s*-\s*evening\b.*$",
        r"\s*-\s*daytime\b.*$",
        r"\s*-\s*sleepover\b.*$",
    ]
    lowered = text.lower()
    for pattern in patterns:
        match = re.search(pattern, lowered)
        if match:
            return text[: match.start()].strip(" -")
    return text


def _extract_option_payload(option) -> Dict[str, str]:
    payload: Dict[str, str] = {}
    keys = [
        "value",
        "title",
        "data-payload",
        "data-json",
        "data-item",
        "data-item-number",
        "data-itemnumber",
        "data-service-code",
        "data-servicecode",
        "data-rate",
        "data-default-rate",
        "data-unit",
        "data-unit-type",
        "data-parent",
        "data-group",
        "data-service-type-id",
        "data-eid",
        "data-id",
        "outerHTML",
    ]
    for key in keys:
        try:
            payload[key] = _normalize_text(option.get_attribute(key) or "")
        except Exception:
            payload[key] = ""
    payload["text"] = _normalize_text(getattr(option, "text", ""))

    for blob_key in ("data-payload", "data-json", "value"):
        parsed = _parse_option_structured_blob(payload.get(blob_key, ""))
        for key, value in parsed.items():
            token = f"blob:{key}"
            if token not in payload or not payload[token]:
                payload[token] = _normalize_text(value)
    return payload


def _is_placeholder_option(payload: Dict[str, str]) -> bool:
    label = _normalize_text(payload.get("text", "")).lower()
    value = _normalize_text(payload.get("value", "")).lower()
    if not label and not value:
        return True
    placeholders = {"select", "--select--", "please select", "choose", "none"}
    if label in placeholders:
        return True
    if value in {"", "0", "-1"} and label in {"", "service type"}:
        return True
    return False


def _parse_discovery_row(payload: Dict[str, str], source_client_id: Optional[str]) -> Dict[str, str]:
    label = _normalize_text(payload.get("text", ""))
    parent = _payload_get(payload, "parent", "group", "blob:parent", "blob:group")
    if not parent:
        parent = _derive_parent_label(label)

    service_type_id = _payload_get(
        payload,
        "service_type_id",
        "serviceTypeId",
        "eid",
        "id",
        "data-service-type-id",
        "data-eid",
        "data-id",
        "blob:service_type_id",
        "blob:eid",
        "blob:id",
    )
    if not service_type_id:
        service_type_id = _extract_id_token(payload.get("value", ""))

    item_number = _payload_get(
        payload,
        "item_number",
        "itemnumber",
        "item",
        "service_code",
        "servicecode",
        "blob:item_number",
        "blob:itemnumber",
        "blob:item",
        "blob:service_code",
        "blob:servicecode",
        "data-item",
        "data-item-number",
        "data-itemnumber",
        "data-service-code",
        "data-servicecode",
    )
    if not item_number:
        item_number = _normalize_item_number(" ".join([label, payload.get("value", ""), payload.get("data-payload", "")]))
    item_number = _normalize_item_number(item_number)

    rate = _payload_get(
        payload,
        "rate",
        "default_rate",
        "price",
        "amount",
        "blob:rate",
        "blob:default_rate",
        "blob:price",
        "blob:amount",
        "data-rate",
        "data-default-rate",
    )
    rate = _coerce_rate_text(rate)

    unit_type = _payload_get(
        payload,
        "unit",
        "unit_type",
        "billing_type",
        "payment_type",
        "blob:unit",
        "blob:unit_type",
        "blob:billing_type",
        "blob:payment_type",
        "data-unit",
        "data-unit-type",
    )

    return {
        "Parent Service Type": parent,
        "Service Variant Label": label,
        "Service Type ID": service_type_id,
        "Item Number": item_number,
        "Service Code": item_number,
        "Rate": rate,
        "Rate Source": "setter_payload" if rate else "",
        "Unit Type": unit_type,
        "Setter Value": _normalize_text(payload.get("value", "")),
        "Payload JSON": _safe_json(payload),
        "Source Client ID": _normalize_text(source_client_id),
        "Captured At (UTC)": _utc_now(),
    }


def _dedupe_discovery_rows(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    chosen: Dict[Tuple[str, str, str], Dict[str, str]] = {}
    for row in rows:
        key = (
            _normalize_text(row.get("Parent Service Type", "")).lower(),
            _normalize_text(row.get("Service Variant Label", "")).lower(),
            _normalize_text(row.get("Service Type ID", "")),
        )
        current = chosen.get(key)
        if current is None:
            chosen[key] = row
            continue

        score_current = int(bool(current.get("Item Number"))) + int(bool(current.get("Rate")))
        score_new = int(bool(row.get("Item Number"))) + int(bool(row.get("Rate")))
        if score_new >= score_current:
            chosen[key] = row

    deduped = list(chosen.values())
    deduped.sort(
        key=lambda item: (
            _normalize_text(item.get("Parent Service Type", "")).lower(),
            _normalize_text(item.get("Service Variant Label", "")).lower(),
            _normalize_text(item.get("Item Number", "")).lower(),
        )
    )
    return deduped


def count_item_number_coverage(rows: List[Dict[str, str]]) -> Dict[str, int]:
    with_item = 0
    missing_item = 0
    for row in rows:
        if _normalize_text(row.get("Item Number", "")):
            with_item += 1
        else:
            missing_item += 1
    return {
        "rows_with_item_number": with_item,
        "rows_missing_item_number": missing_item,
    }


class DiagnosticsRecorder:
    def __init__(
        self,
        *,
        run_id: str,
        folder: Path,
        on_event: EventCallback = None,
        on_progress: ProgressCallback = None,
    ):
        self.run_id = run_id
        self.folder = folder
        self.on_event = on_event
        self.on_progress = on_progress
        self.events_path = folder / "events.jsonl"
        self.checkers_path = folder / "checkers.csv"
        self.summary_path = folder / "summary.json"
        self._init_checker_csv()
        self.events_count = 0
        self.checker_count = 0

    def _init_checker_csv(self):
        with self.checkers_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CHECKER_FIELDS)
            writer.writeheader()

    def emit_event(
        self,
        *,
        level: str,
        step: str,
        code: str,
        message: str,
        client_id: str = "",
        url: str = "",
        selector: str = "",
        option_count: Optional[int] = None,
        context: Optional[Dict[str, object]] = None,
    ):
        event = {
            "run_id": self.run_id,
            "ts_utc": _utc_now(),
            "level": _normalize_text(level).upper() or "INFO",
            "step": _normalize_text(step),
            "code": _normalize_text(code),
            "message": _normalize_text(message),
            "client_id": _normalize_text(client_id),
            "url": _normalize_text(url),
            "selector": _normalize_text(selector),
            "option_count": "" if option_count is None else str(option_count),
            "context_json": _safe_json(context or {}),
        }
        with self.events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.events_count += 1

        human = f"[ItemDiscovery][{event['level']}][{event['code']}] {event['message']}"
        _emit(human, self.on_progress)
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:
                pass

    def capture_artifacts(self, driver, *, step: str, code: str) -> Tuple[str, str]:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_step = re.sub(r"[^a-zA-Z0-9_]+", "_", step).strip("_") or "step"
        safe_code = re.sub(r"[^a-zA-Z0-9_]+", "_", code).strip("_") or "code"
        screenshot = self.folder / f"{stamp}_{safe_step}_{safe_code}.png"
        html_path = self.folder / f"{stamp}_{safe_step}_{safe_code}.html"

        screenshot_out = ""
        html_out = ""
        try:
            driver.save_screenshot(str(screenshot))
            screenshot_out = str(screenshot)
        except Exception:
            screenshot_out = ""
        try:
            html = driver.page_source or ""
            html_path.write_text(html, encoding="utf-8")
            html_out = str(html_path)
        except Exception:
            html_out = ""
        return screenshot_out, html_out

    def emit_checker(
        self,
        *,
        step: str,
        status: str,
        code: str,
        message: str,
        client_id: str = "",
        url: str = "",
        selector: str = "",
        option_count: Optional[int] = None,
        artifact_screenshot: str = "",
        artifact_html: str = "",
    ):
        row = {
            "run_id": self.run_id,
            "ts_utc": _utc_now(),
            "step": _normalize_text(step),
            "status": _normalize_text(status).lower(),
            "code": _normalize_text(code),
            "message": _normalize_text(message),
            "client_id": _normalize_text(client_id),
            "url": _normalize_text(url),
            "selector": _normalize_text(selector),
            "option_count": "" if option_count is None else str(option_count),
            "artifact_screenshot": _normalize_text(artifact_screenshot),
            "artifact_html": _normalize_text(artifact_html),
        }
        with self.checkers_path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CHECKER_FIELDS)
            writer.writerow(row)
        self.checker_count += 1

    def write_summary(self, summary: Dict[str, object]):
        data = dict(summary)
        data.setdefault("run_id", self.run_id)
        data.setdefault("events_count", self.events_count)
        data.setdefault("checker_count", self.checker_count)
        self.summary_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _navigate_and_wait_body(driver, url: str, timeout: int = 20) -> bool:
    try:
        driver.get(url)
        wait_for(driver, By.TAG_NAME, "body", timeout=timeout)
        return True
    except Exception:
        return False


def _try_open_client_context(driver, recorder: DiagnosticsRecorder, client_id: Optional[str]) -> bool:
    if not client_id:
        recorder.emit_checker(
            step=CHECKER_CLIENT,
            status="pass",
            code=CHECKER_CLIENT,
            message="No probe client supplied; continuing with global appointment discovery.",
            client_id="",
            url=_normalize_text(getattr(driver, "current_url", "")),
        )
        return True

    for url in _client_url_candidates(client_id):
        if _navigate_and_wait_body(driver, url):
            if _is_logged_in(driver):
                recorder.emit_checker(
                    step=CHECKER_CLIENT,
                    status="pass",
                    code=CHECKER_CLIENT,
                    message=f"Client context opened at {url}",
                    client_id=client_id,
                    url=url,
                )
                recorder.emit_event(
                    level="INFO",
                    step=CHECKER_CLIENT,
                    code=CHECKER_CLIENT,
                    message="Client page reached.",
                    client_id=client_id,
                    url=url,
                )
                return True

    screenshot, html = recorder.capture_artifacts(driver, step=CHECKER_CLIENT, code=CHECKER_CLIENT)
    recorder.emit_checker(
        step=CHECKER_CLIENT,
        status="fail",
        code=CHECKER_CLIENT,
        message=f"Client context unavailable for client {client_id}",
        client_id=client_id,
        url=_normalize_text(getattr(driver, "current_url", "")),
        artifact_screenshot=screenshot,
        artifact_html=html,
    )
    recorder.emit_event(
        level="ERROR",
        step=CHECKER_CLIENT,
        code=CHECKER_CLIENT,
        message=f"Client page open failed for {client_id}.",
        client_id=client_id,
        url=_normalize_text(getattr(driver, "current_url", "")),
    )
    return False


def _try_open_appointment_entry(driver, recorder: DiagnosticsRecorder, client_id: Optional[str]) -> bool:
    for url in _appointment_url_candidates(client_id):
        if _navigate_and_wait_body(driver, url):
            body = _body_text(driver).lower()
            if _is_logged_in(driver) and not _is_404_page(driver):
                looks_ready = (
                    ("appointment details" in body and "service type" in body)
                    or ("add appointment" in body and "service type" in body)
                )
                if not looks_ready:
                    continue
                recorder.emit_checker(
                    step=CHECKER_APPOINTMENT,
                    status="pass",
                    code=CHECKER_APPOINTMENT,
                    message=f"Appointment entry reached at {url}",
                    client_id=_normalize_text(client_id),
                    url=url,
                )
                recorder.emit_event(
                    level="INFO",
                    step=CHECKER_APPOINTMENT,
                    code=CHECKER_APPOINTMENT,
                    message="Appointment page reached.",
                    client_id=_normalize_text(client_id),
                    url=url,
                )
                return True

    screenshot, html = recorder.capture_artifacts(driver, step=CHECKER_APPOINTMENT, code=CHECKER_APPOINTMENT)
    recorder.emit_checker(
        step=CHECKER_APPOINTMENT,
        status="fail",
        code=CHECKER_APPOINTMENT,
        message="Could not reach appointment entry page.",
        client_id=_normalize_text(client_id),
        url=_normalize_text(getattr(driver, "current_url", "")),
        artifact_screenshot=screenshot,
        artifact_html=html,
    )
    recorder.emit_event(
        level="ERROR",
        step=CHECKER_APPOINTMENT,
        code=CHECKER_APPOINTMENT,
        message="Appointment page open failed.",
        client_id=_normalize_text(client_id),
        url=_normalize_text(getattr(driver, "current_url", "")),
    )
    return False


def _inspect_dropdown(driver, recorder: DiagnosticsRecorder, client_id: Optional[str]):
    dropdown, selector_used = _find_service_type_dropdown(driver)
    if dropdown is None:
        screenshot, html = recorder.capture_artifacts(
            driver,
            step=CHECKER_DROPDOWN_PRESENT,
            code=CHECKER_DROPDOWN_PRESENT,
        )
        recorder.emit_checker(
            step=CHECKER_DROPDOWN_PRESENT,
            status="fail",
            code=CHECKER_DROPDOWN_PRESENT,
            message="Service type dropdown was not found.",
            client_id=_normalize_text(client_id),
            url=_normalize_text(getattr(driver, "current_url", "")),
            selector=selector_used,
            artifact_screenshot=screenshot,
            artifact_html=html,
        )
        recorder.emit_event(
            level="ERROR",
            step=CHECKER_DROPDOWN_PRESENT,
            code=CHECKER_DROPDOWN_PRESENT,
            message="Dropdown lookup failed.",
            client_id=_normalize_text(client_id),
            url=_normalize_text(getattr(driver, "current_url", "")),
            selector=selector_used,
        )
        return None, selector_used, []

    recorder.emit_checker(
        step=CHECKER_DROPDOWN_PRESENT,
        status="pass",
        code=CHECKER_DROPDOWN_PRESENT,
        message="Service type dropdown located.",
        client_id=_normalize_text(client_id),
        url=_normalize_text(getattr(driver, "current_url", "")),
        selector=selector_used,
    )

    is_visible = False
    is_enabled = False
    try:
        is_visible = bool(dropdown.is_displayed())
    except Exception:
        is_visible = False
    try:
        is_enabled = bool(dropdown.is_enabled())
    except Exception:
        is_enabled = False

    if not (is_visible and is_enabled):
        screenshot, html = recorder.capture_artifacts(
            driver,
            step=CHECKER_DROPDOWN_STATE,
            code=CHECKER_DROPDOWN_STATE,
        )
        recorder.emit_checker(
            step=CHECKER_DROPDOWN_STATE,
            status="fail",
            code=CHECKER_DROPDOWN_STATE,
            message="Dropdown located but not visible/enabled.",
            client_id=_normalize_text(client_id),
            url=_normalize_text(getattr(driver, "current_url", "")),
            selector=selector_used,
            artifact_screenshot=screenshot,
            artifact_html=html,
        )
        recorder.emit_event(
            level="ERROR",
            step=CHECKER_DROPDOWN_STATE,
            code=CHECKER_DROPDOWN_STATE,
            message="Dropdown is not interactable.",
            client_id=_normalize_text(client_id),
            url=_normalize_text(getattr(driver, "current_url", "")),
            selector=selector_used,
        )
        return None, selector_used, []

    recorder.emit_checker(
        step=CHECKER_DROPDOWN_STATE,
        status="pass",
        code=CHECKER_DROPDOWN_STATE,
        message="Dropdown is visible and enabled.",
        client_id=_normalize_text(client_id),
        url=_normalize_text(getattr(driver, "current_url", "")),
        selector=selector_used,
    )

    try:
        option_elements = Select(dropdown).options
    except Exception:
        option_elements = []

    recorder.emit_checker(
        step=CHECKER_DROPDOWN_OPTIONS,
        status="pass",
        code=CHECKER_DROPDOWN_OPTIONS,
        message=f"Dropdown options extracted ({len(option_elements)} raw options).",
        client_id=_normalize_text(client_id),
        url=_normalize_text(getattr(driver, "current_url", "")),
        selector=selector_used,
        option_count=len(option_elements),
    )

    non_placeholder = []
    for option in option_elements:
        payload = _extract_option_payload(option)
        if _is_placeholder_option(payload):
            continue
        non_placeholder.append(payload)

    if not non_placeholder:
        screenshot, html = recorder.capture_artifacts(
            driver,
            step=CHECKER_DROPDOWN_NONEMPTY,
            code=CHECKER_DROPDOWN_EMPTY,
        )
        recorder.emit_checker(
            step=CHECKER_DROPDOWN_NONEMPTY,
            status="warn",
            code=CHECKER_DROPDOWN_EMPTY,
            message="Dropdown resolved but no usable values were present.",
            client_id=_normalize_text(client_id),
            url=_normalize_text(getattr(driver, "current_url", "")),
            selector=selector_used,
            option_count=0,
            artifact_screenshot=screenshot,
            artifact_html=html,
        )
        page_title = _normalize_text(getattr(driver, "title", ""))
        recorder.emit_event(
            level="WARN",
            step=CHECKER_DROPDOWN_NONEMPTY,
            code=CHECKER_DROPDOWN_EMPTY,
            message="Dropdown empty; continuing to next probe.",
            client_id=_normalize_text(client_id),
            url=_normalize_text(getattr(driver, "current_url", "")),
            selector=selector_used,
            option_count=0,
            context={
                "page_title": page_title,
                "selector_attempted": selector_used,
            },
        )
        return dropdown, selector_used, []

    recorder.emit_checker(
        step=CHECKER_DROPDOWN_NONEMPTY,
        status="pass",
        code=CHECKER_DROPDOWN_NONEMPTY,
        message=f"Dropdown usable options: {len(non_placeholder)}",
        client_id=_normalize_text(client_id),
        url=_normalize_text(getattr(driver, "current_url", "")),
        selector=selector_used,
        option_count=len(non_placeholder),
    )
    return dropdown, selector_used, non_placeholder


def _inspect_assist_service_options(
    driver,
    recorder: DiagnosticsRecorder,
    client_id: str,
    *,
    seed_query: str = "a",
    discovery_debug: bool = False,
) -> List[Dict[str, str]]:
    client_input, client_hidden, client_options = _collect_assist_options(
        driver,
        container_id="client_id",
        seed_query=seed_query,
        query_candidates=[client_id, seed_query, ""],
        recorder=recorder,
        client_id=client_id,
        present_checker=CHECKER_CLIENT_COMBO_PRESENT,
        options_checker=CHECKER_CLIENT_OPTIONS_NONEMPTY,
        discovery_debug=discovery_debug,
    )
    selected_client = False
    selected_client_id = ""
    if client_input is not None and client_hidden is not None and client_options:
        selected_client = _select_first_assist_option(driver, client_input, client_hidden)
        if selected_client:
            selected_client_id = _normalize_text(client_hidden.get_attribute("value") or "")
            recorder.emit_event(
                level="INFO",
                step=CHECKER_CLIENT_OPTIONS_NONEMPTY,
                code=CHECKER_CLIENT_OPTIONS_NONEMPTY,
                message="Assist client selected.",
                client_id=_normalize_text(client_id),
                url=_normalize_text(getattr(driver, "current_url", "")),
                context={"selected_client_id": selected_client_id},
            )
        else:
            screenshot, html = recorder.capture_artifacts(
                driver, step=CHECKER_CLIENT_OPTIONS_NONEMPTY, code=CHECKER_CLIENT_OPTIONS_NONEMPTY
            )
            recorder.emit_checker(
                step=CHECKER_CLIENT_OPTIONS_NONEMPTY,
                status="fail",
                code=CHECKER_CLIENT_OPTIONS_NONEMPTY,
                message="Could not select client from Assist client options.",
                client_id=_normalize_text(client_id),
                url=_normalize_text(getattr(driver, "current_url", "")),
                artifact_screenshot=screenshot,
                artifact_html=html,
            )
            recorder.emit_event(
                level="ERROR",
                step=CHECKER_CLIENT_OPTIONS_NONEMPTY,
                code=CHECKER_CLIENT_OPTIONS_NONEMPTY,
                message="Assist client selection failed; attempting service extraction without selected client.",
                client_id=_normalize_text(client_id),
                url=_normalize_text(getattr(driver, "current_url", "")),
            )
    else:
        recorder.emit_event(
            level="WARN",
            step=CHECKER_CLIENT_OPTIONS_NONEMPTY,
            code=CHECKER_DROPDOWN_EMPTY,
            message="Assist client options unavailable; attempting service extraction without selected client.",
            client_id=_normalize_text(client_id),
            url=_normalize_text(getattr(driver, "current_url", "")),
        )

    _, _, service_options = _collect_assist_options(
        driver,
        container_id="service_type_id",
        seed_query=seed_query,
        query_candidates=["", seed_query],
        recorder=recorder,
        client_id=client_id,
        present_checker=CHECKER_SERVICE_COMBO_PRESENT,
        options_checker=CHECKER_SERVICE_OPTIONS_NONEMPTY,
        discovery_debug=discovery_debug,
    )
    if not service_options:
        return []

    payloads = []
    for option in service_options:
        label = _normalize_text(option.get("label", ""))
        value = _normalize_text(option.get("value", ""))
        if not value:
            recorder.emit_event(
                level="WARN",
                step=CHECKER_SERVICE_OPTIONS_NONEMPTY,
                code="ASSIST_OPTION_MISSING_VALUE",
                message="Service option is missing value/service_type_id; row skipped.",
                client_id=_normalize_text(client_id),
                url=_normalize_text(getattr(driver, "current_url", "")),
                context={"label": label, "selected_client_id": selected_client_id},
            )
            continue
        payloads.append(
            {
                "text": label,
                "value": value,
                "service_type_id": value,
                "data-source": "assist_react_options",
                "selected_client_id": selected_client_id if selected_client else "",
            }
        )
    return payloads


def _fetch_service_type_details(
    driver,
    recorder: DiagnosticsRecorder,
    *,
    service_type_id: str,
    source_client_id: str,
    cache: Dict[str, Dict[str, str]],
) -> Dict[str, str]:
    service_type_id = _normalize_text(service_type_id)
    if not service_type_id:
        return {"item_number": "", "rate": "", "ok": "0"}
    if service_type_id in cache:
        return cache[service_type_id]

    url = f"{BASE_URL.rstrip('/')}/service-type-details.asp?eid={service_type_id}"
    details = {"item_number": "", "rate": "", "ok": "0"}
    if not _navigate_and_wait_body(driver, url, timeout=30) or _is_404_page(driver):
        screenshot, html = recorder.capture_artifacts(
            driver, step=CHECKER_DETAILS_PAGE_OPEN, code=CHECKER_DETAILS_PAGE_OPEN
        )
        recorder.emit_checker(
            step=CHECKER_DETAILS_PAGE_OPEN,
            status="fail",
            code=CHECKER_DETAILS_PAGE_OPEN,
            message=f"Service type details page unavailable for eid={service_type_id}.",
            client_id=source_client_id,
            url=_normalize_text(getattr(driver, "current_url", "")),
            selector=f"eid={service_type_id}",
            artifact_screenshot=screenshot,
            artifact_html=html,
        )
        recorder.emit_event(
            level="ERROR",
            step=CHECKER_DETAILS_PAGE_OPEN,
            code=CHECKER_DETAILS_PAGE_OPEN,
            message=f"Details page open failed for eid={service_type_id}.",
            client_id=source_client_id,
            url=_normalize_text(getattr(driver, "current_url", "")),
        )
        cache[service_type_id] = details
        return details

    recorder.emit_checker(
        step=CHECKER_DETAILS_PAGE_OPEN,
        status="pass",
        code=CHECKER_DETAILS_PAGE_OPEN,
        message=f"Service type details opened for eid={service_type_id}.",
        client_id=source_client_id,
        url=url,
        selector=f"eid={service_type_id}",
    )

    item_number = ""
    rate = ""
    try:
        item_number = _normalize_item_number(
            driver.find_element(By.NAME, "ef581").get_attribute("value") or ""
        )
    except Exception:
        item_number = ""
    try:
        rate = _coerce_rate_text(
            driver.find_element(By.NAME, "ef592").get_attribute("value") or ""
        )
    except Exception:
        rate = ""

    details = {
        "item_number": item_number,
        "rate": rate,
        "ok": "1",
    }
    if item_number:
        recorder.emit_checker(
            step=CHECKER_ITEM_NUMBER_RESOLVED,
            status="pass",
            code=CHECKER_ITEM_NUMBER_RESOLVED,
            message=f"Item number resolved for eid={service_type_id}.",
            client_id=source_client_id,
            url=url,
            selector=f"eid={service_type_id}",
        )
    else:
        screenshot, html = recorder.capture_artifacts(
            driver, step=CHECKER_ITEM_NUMBER_RESOLVED, code=CHECKER_ITEM_NUMBER_RESOLVED
        )
        recorder.emit_checker(
            step=CHECKER_ITEM_NUMBER_RESOLVED,
            status="warn",
            code=CHECKER_ITEM_NUMBER_RESOLVED,
            message=f"Item number missing in details page for eid={service_type_id}.",
            client_id=source_client_id,
            url=url,
            selector=f"eid={service_type_id}",
            artifact_screenshot=screenshot,
            artifact_html=html,
        )
    cache[service_type_id] = details
    return details


def discover_appointment_item_numbers(
    headless: bool = True,
    probe_client_id=None,
    on_progress: ProgressCallback = None,
    on_event: EventCallback = None,
    discovery_debug: bool = False,
) -> Dict[str, object]:
    ensure_credentials()
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root, diagnostics_folder = _ensure_output_paths(run_id)
    recorder = DiagnosticsRecorder(
        run_id=run_id,
        folder=diagnostics_folder,
        on_event=on_event,
        on_progress=on_progress,
    )
    probe_clients = _parse_probe_clients(probe_client_id)
    rows: List[Dict[str, str]] = []
    counters = {
        "total_options_seen": 0,
        "rows_with_item_number": 0,
        "rows_missing_item_number": 0,
        "dropdown_empty_count": 0,
        "probe_count": len(probe_clients),
        "probes_processed": 0,
        "details_pages_queried": 0,
        "item_number_resolved_count": 0,
        "item_number_missing_after_details_count": 0,
        "assist_mode_used": False,
        "legacy_fallback_used": False,
    }
    started_at = _utc_now()

    recorder.emit_event(
        level="INFO",
        step="RUN_START",
        code="RUN_START",
        message="Appointment item discovery started.",
        context={
            "probe_clients": probe_clients,
            "headless": bool(headless),
            "discovery_debug": bool(discovery_debug),
        },
    )

    # The existing driver builder expects an output dir; diagnostics folder is used.
    driver = build_chrome_driver(headless=headless, download_dir=diagnostics_folder)
    try:
        login(driver)
        details_cache: Dict[str, Dict[str, str]] = {}

        for probe in probe_clients:
            counters["probes_processed"] += 1
            probe_id = _normalize_text(probe)
            recorder.emit_event(
                level="INFO",
                step="PROBE_START",
                code="PROBE_START",
                message=f"Probe started for client '{probe_id or 'GLOBAL'}'.",
                client_id=probe_id,
            )

            option_payloads: List[Dict[str, str]] = []
            route_mode = "legacy"

            assist_ready = _open_assist_appointments_new(driver, recorder)
            if assist_ready:
                route_mode = "assist"
                counters["assist_mode_used"] = True
                if not probe_id:
                    screenshot, html = recorder.capture_artifacts(
                        driver,
                        step=CHECKER_CLIENT_COMBO_PRESENT,
                        code=CHECKER_CLIENT_COMBO_PRESENT,
                    )
                    recorder.emit_checker(
                        step=CHECKER_CLIENT_COMBO_PRESENT,
                        status="fail",
                        code=CHECKER_CLIENT_COMBO_PRESENT,
                        message="Assist mode requires explicit probe client id.",
                        client_id="",
                        url=_normalize_text(getattr(driver, "current_url", "")),
                        artifact_screenshot=screenshot,
                        artifact_html=html,
                    )
                    raise RuntimeError(
                        "Assist mode requires --probe-client-id. "
                        "Example: --discover-item-numbers --probe-client-id 56851"
                    )
                option_payloads = _inspect_assist_service_options(
                    driver,
                    recorder,
                    probe_id,
                    seed_query="a",
                    discovery_debug=discovery_debug,
                )
                if not option_payloads:
                    counters["dropdown_empty_count"] += 1
                    continue
            else:
                counters["legacy_fallback_used"] = True
                recorder.emit_event(
                    level="WARN",
                    step=CHECKER_ROUTE_ASSIST,
                    code=CHECKER_ROUTE_ASSIST,
                    message="Assist route unavailable; falling back to legacy appointment flow.",
                    client_id=probe_id,
                    url=_normalize_text(getattr(driver, "current_url", "")),
                )
                has_client = _try_open_client_context(driver, recorder, probe_id or None)
                if not has_client and probe_id:
                    continue

                has_appointment = _try_open_appointment_entry(driver, recorder, probe_id or None)
                if not has_appointment:
                    continue

                dropdown, selector_used, option_payloads = _inspect_dropdown(
                    driver,
                    recorder,
                    probe_id or None,
                )
                if dropdown is None:
                    continue
                if not option_payloads:
                    counters["dropdown_empty_count"] += 1
                    continue

            for index, payload in enumerate(option_payloads, start=1):
                counters["total_options_seen"] += 1
                row = _parse_discovery_row(payload, probe_id or None)
                rows.append(row)
                if row.get("Item Number"):
                    counters["rows_with_item_number"] += 1
                else:
                    counters["rows_missing_item_number"] += 1

                if discovery_debug:
                    recorder.emit_event(
                        level="DEBUG",
                        step="ROW_PARSE",
                        code="ROW_PARSED",
                        message=f"Parsed option row #{index}.",
                        client_id=probe_id,
                        url=_normalize_text(getattr(driver, "current_url", "")),
                        selector=route_mode,
                        option_count=index,
                        context={
                            "has_item_number": bool(row.get("Item Number")),
                            "has_rate": bool(row.get("Rate")),
                            "service_variant": row.get("Service Variant Label", ""),
                            "parent_service_type": row.get("Parent Service Type", ""),
                            "route_mode": route_mode,
                        },
                    )

        rows = _dedupe_discovery_rows(rows)
        # Resolve item number + rate from service type details page via service_type_id.
        for row in rows:
            service_type_id = _normalize_text(row.get("Service Type ID", ""))
            if not service_type_id:
                continue
            details = _fetch_service_type_details(
                driver,
                recorder,
                service_type_id=service_type_id,
                source_client_id=_normalize_text(row.get("Source Client ID", "")),
                cache=details_cache,
            )
            item = _normalize_text(details.get("item_number", ""))
            rate = _normalize_text(details.get("rate", ""))
            if item:
                row["Item Number"] = item
                row["Service Code"] = item
            if not row.get("Rate") and rate:
                row["Rate"] = rate
                row["Rate Source"] = "service_type_details"

        coverage = count_item_number_coverage(rows)
        counters["rows_with_item_number"] = coverage["rows_with_item_number"]
        counters["rows_missing_item_number"] = coverage["rows_missing_item_number"]
        counters["details_pages_queried"] = len(details_cache)
        counters["item_number_resolved_count"] = coverage["rows_with_item_number"]
        counters["item_number_missing_after_details_count"] = coverage["rows_missing_item_number"]
        saved = _save_dataset_outputs(
            rows,
            DISCOVERY_COLUMNS,
            output_root,
            prefix="AppointmentItemDiscovery",
            latest_name="AppointmentItemDiscovery_latest.csv",
            progress=on_progress,
        )

        summary = {
            "started_at": started_at,
            "finished_at": _utc_now(),
            "run_id": run_id,
            "counters": counters,
            "assist_mode_used": counters["assist_mode_used"],
            "legacy_fallback_used": counters["legacy_fallback_used"],
            "details_pages_queried": counters["details_pages_queried"],
            "item_number_resolved_count": counters["item_number_resolved_count"],
            "item_number_missing_after_details_count": counters["item_number_missing_after_details_count"],
            "row_count": len(rows),
            "output_root": str(output_root),
            "diagnostics_folder": str(diagnostics_folder),
            "discovery_csv_path": str(saved["csv_path"]),
            "discovery_xlsx_path": str(saved["xlsx_path"]),
            "discovery_latest_csv": str(saved["latest_csv"]),
            "discovery_latest_xlsx": str(saved["latest_xlsx"]),
        }
        recorder.write_summary(summary)
        recorder.emit_event(
            level="INFO",
            step="RUN_END",
            code="RUN_END",
            message=f"Appointment item discovery completed with {len(rows)} row(s).",
            context=counters,
        )
        return {
            "rows": rows,
            "row_count": len(rows),
            "run_id": run_id,
            "diagnostics_folder": diagnostics_folder,
            "events_path": recorder.events_path,
            "checkers_path": recorder.checkers_path,
            "summary_path": recorder.summary_path,
            **saved,
            **counters,
        }
    except TimeoutException as exc:
        screenshot, html = recorder.capture_artifacts(driver, step="RUN_ERROR", code="TIMEOUT")
        recorder.emit_event(
            level="ERROR",
            step="RUN_ERROR",
            code="TIMEOUT",
            message=f"Discovery timed out: {exc}",
            url=_normalize_text(getattr(driver, "current_url", "")),
            context={"screenshot": screenshot, "html": html},
        )
        raise
    except Exception as exc:
        screenshot, html = recorder.capture_artifacts(driver, step="RUN_ERROR", code="UNHANDLED")
        recorder.emit_event(
            level="ERROR",
            step="RUN_ERROR",
            code="UNHANDLED",
            message=f"Discovery failed: {exc}",
            url=_normalize_text(getattr(driver, "current_url", "")),
            context={"screenshot": screenshot, "html": html},
        )
        raise
    finally:
        driver.quit()


def _best_discovery_row(existing: Optional[Dict[str, str]], candidate: Dict[str, str]) -> Dict[str, str]:
    if existing is None:
        return candidate
    score_existing = int(bool(existing.get("Item Number"))) + int(bool(existing.get("Rate")))
    score_candidate = int(bool(candidate.get("Item Number"))) + int(bool(candidate.get("Rate")))
    if score_candidate >= score_existing:
        return candidate
    return existing


def merge_discovery_with_service_types(
    reference_rows: List[Dict[str, str]],
    discovered_rows: List[Dict[str, str]],
) -> Dict[str, object]:
    label_map: Dict[str, Dict[str, str]] = {}
    id_map: Dict[str, Dict[str, str]] = {}
    matched_discovery_keys: set[str] = set()

    for row in discovered_rows:
        label_key = _normalize_text(row.get("Service Variant Label", "")).lower()
        id_key = _normalize_text(row.get("Service Type ID", ""))
        if label_key:
            label_map[label_key] = _best_discovery_row(label_map.get(label_key), row)
        if id_key:
            id_map[id_key] = _best_discovery_row(id_map.get(id_key), row)

    enriched_rows: List[Dict[str, str]] = []
    for ref in reference_rows:
        result = {column: _normalize_text(ref.get(column, "")) for column in DATASET_COLUMNS}
        variant_label = _normalize_text(result.get("Service Type", ""))
        ref_id = _normalize_text(result.get("ID", ""))
        matched = label_map.get(variant_label.lower()) or id_map.get(ref_id)
        if matched:
            key = f"{_normalize_text(matched.get('Service Variant Label','')).lower()}|{_normalize_text(matched.get('Service Type ID',''))}"
            matched_discovery_keys.add(key)

        discovered_item = _normalize_text(matched.get("Item Number", "")) if matched else ""
        existing_service_code = _normalize_text(result.get("Service Code", ""))
        final_item = discovered_item or existing_service_code
        final_rate = _normalize_text(matched.get("Rate", "")) if matched else ""
        rate_source = "setter_payload" if final_rate else ""
        if not final_rate:
            final_rate = _normalize_text(result.get("Def. Rate", ""))
            rate_source = "fallback_service_types" if final_rate else ""

        result["Service Code"] = final_item
        enriched = {
            **result,
            "Item Number": final_item,
            "Rate": final_rate,
            "Rate Source": rate_source,
            "Discovery Parent Service Type": _normalize_text(matched.get("Parent Service Type", "")) if matched else "",
            "Discovery Service Variant": _normalize_text(matched.get("Service Variant Label", "")) if matched else "",
            "Discovery Setter Value": _normalize_text(matched.get("Setter Value", "")) if matched else "",
            "Discovery Source Client ID": _normalize_text(matched.get("Source Client ID", "")) if matched else "",
            "Discovery Captured At (UTC)": _normalize_text(matched.get("Captured At (UTC)", "")) if matched else "",
            "Discovery Payload JSON": _normalize_text(matched.get("Payload JSON", "")) if matched else "",
        }
        enriched_rows.append(enriched)

    unmatched: List[Dict[str, str]] = []
    for row in discovered_rows:
        key = f"{_normalize_text(row.get('Service Variant Label','')).lower()}|{_normalize_text(row.get('Service Type ID',''))}"
        if key not in matched_discovery_keys:
            unmatched.append({column: _normalize_text(row.get(column, "")) for column in DISCOVERY_COLUMNS})

    return {
        "enriched_rows": enriched_rows,
        "unmatched_rows": unmatched,
        "enriched_count": len(enriched_rows),
        "unmatched_count": len(unmatched),
    }


def run_service_type_merge(
    discovered_rows: List[Dict[str, str]],
    *,
    reference_csv: Path = REFERENCE_LATEST_CSV,
    output_root: Optional[Path] = None,
    progress: ProgressCallback = None,
) -> Dict[str, object]:
    target_root = output_root or (ARCHIVE_ROOT / "ServiceTypeRateExtractor").resolve()
    target_root.mkdir(parents=True, exist_ok=True)
    reference_rows = _load_csv_rows(reference_csv)
    merged = merge_discovery_with_service_types(reference_rows, discovered_rows)

    enriched_saved = _save_dataset_outputs(
        merged["enriched_rows"],
        ENRICHED_COLUMNS,
        target_root,
        prefix="ServiceTypes_enriched",
        latest_name="ServiceTypes_enriched_latest.csv",
        progress=progress,
    )
    unmatched_saved = _save_dataset_outputs(
        merged["unmatched_rows"],
        DISCOVERY_COLUMNS,
        target_root,
        prefix="ServiceTypes_unmatched_discovery",
        latest_name="ServiceTypes_unmatched_discovery_latest.csv",
        progress=progress,
    )
    return {
        **merged,
        "reference_csv": reference_csv,
        "enriched_csv_path": enriched_saved["csv_path"],
        "enriched_xlsx_path": enriched_saved["xlsx_path"],
        "enriched_latest_csv": enriched_saved["latest_csv"],
        "enriched_latest_xlsx": enriched_saved["latest_xlsx"],
        "unmatched_csv_path": unmatched_saved["csv_path"],
        "unmatched_xlsx_path": unmatched_saved["xlsx_path"],
        "unmatched_latest_csv": unmatched_saved["latest_csv"],
        "unmatched_latest_xlsx": unmatched_saved["latest_xlsx"],
    }


def load_discovery_latest(path: Path = DISCOVERY_LATEST_CSV) -> List[Dict[str, str]]:
    return _load_csv_rows(path)

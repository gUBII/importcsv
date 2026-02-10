import re
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.select import Select

from importcsv import build_chrome_driver, ensure_credentials, login, log_message
from selenium_helpers import click_js, retry, wait_for

SERVICE_TYPE_RATES_COLUMNS = (
    "service_type",
    "service",
    "rate",
    "line_item_number",
)

TURNPOINT_DASHBOARD_URL = "https://tp1.com.au/dashboard.asp?hide2=no#map"
TURNPOINT_CLIENTS_URL = "https://tp1.com.au/clients.asp?posted=yes"
DEFAULT_ANCHOR_CLIENT_ID = "92108"
TURNPOINT_CLIENT_DETAILS_URL_TMPL = "https://tp1.com.au/client-details.asp?eid={client_id}"
TURNPOINT_CLIENT_APPOINTMENTS_URL_TMPL = (
    "https://tp1.com.au/client-details.asp?eid={client_id}&BREAKDOWN_SHOW_APPTS=yes&wide1=yes"
)
TEMP_DOWNLOAD_DIR = (
    Path.home() / ".turnpoint_purger" / "service_type_rate_extractor" / "_downloads"
)

PLACEHOLDER_LABELS = {
    "",
    "-",
    "--",
    "---",
    "select",
    "select...",
    "please select",
    "please choose",
}

ProgressCallback = Optional[Callable[[str], None]]
RowCallback = Optional[Callable[[Dict[str, str]], None]]


def _emit(message: str, callback: ProgressCallback = None):
    log_message(message)
    if callback:
        try:
            callback(message)
        except Exception:
            pass


def _normalize_token(text: str) -> str:
    text = (text or "").strip().lower()
    return re.sub(r"\s+", " ", text)


def _is_placeholder_label(label: str) -> bool:
    token = _normalize_token(label)
    return token in PLACEHOLDER_LABELS


def _first_displayed(elements):
    for element in elements:
        try:
            if element.is_displayed():
                return element
        except Exception:
            continue
    return None


def _find_native_service_type_select(driver):
    selectors = [
        (
            By.XPATH,
            "//label[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'service type')]/following::select[1]",
        ),
        (
            By.XPATH,
            "//select[contains(translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'service') and "
            "contains(translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'type')]",
        ),
        (
            By.XPATH,
            "//select[contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'service') and "
            "contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'type')]",
        ),
        (
            By.XPATH,
            "//select[contains(translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'servicetype')]",
        ),
        (
            By.XPATH,
            "//select[contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'servicetype')]",
        ),
    ]
    for by, locator in selectors:
        elems = driver.find_elements(by, locator)
        elem = _first_displayed(elems)
        if elem is not None:
            return elem
    return None


def _find_custom_service_type_trigger(driver):
    selectors = [
        (
            By.XPATH,
            "//*[@role='combobox' and (contains(translate(@aria-label,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'service') "
            "or contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'service'))]",
        ),
        (
            By.XPATH,
            "//label[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'service type')]/"
            "following::*[(self::div or self::span or self::input or self::button) and "
            "(@role='combobox' or contains(@class,'select') or contains(@class,'dropdown'))][1]",
        ),
        (
            By.XPATH,
            "//input[contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'service') and "
            "(contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'select2') or @role='combobox')]",
        ),
        (
            By.XPATH,
            "//div[contains(@class,'select2') and "
            "(contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'service') or "
            "contains(translate(@class,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'service'))]",
        ),
    ]
    for by, locator in selectors:
        elems = driver.find_elements(by, locator)
        elem = _first_displayed(elems)
        if elem is not None:
            return elem
    return None


def _find_add_appointment_button(driver):
    selectors = [
        (
            By.XPATH,
            "//a[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'add appointment')]",
        ),
        (
            By.XPATH,
            "//button[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'add appointment')]",
        ),
        (
            By.XPATH,
            "//input[( @type='button' or @type='submit') and contains(translate(@value,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'add appointment')]",
        ),
        (
            By.XPATH,
            "//a[contains(translate(@href,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'appointment') and "
            "contains(translate(@href,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'add')]",
        ),
        (
            By.XPATH,
            "//*[@onclick and contains(translate(@onclick,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'appointment') and "
            "contains(translate(@onclick,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'add')]",
        ),
    ]
    for by, locator in selectors:
        elems = driver.find_elements(by, locator)
        elem = _first_displayed(elems)
        if elem is not None:
            return elem
    return None


def _find_rate_table(driver):
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
        header_texts = [_normalize_token(h.text) for h in headers]
        if not header_texts:
            continue
        has_service = any("service" in header for header in header_texts)
        has_rate = any("rate" in header for header in header_texts)
        has_code = any("code" in header for header in header_texts)
        if has_service and has_rate and has_code:
            return table
    return None


def _header_index_map(table) -> Optional[Dict[str, int]]:
    headers = table.find_elements(By.XPATH, ".//thead//th|.//thead//td")
    if not headers:
        headers = table.find_elements(By.XPATH, ".//tr[1]/th|.//tr[1]/td")
    if not headers:
        return None
    index_map = {}
    for idx, header in enumerate(headers):
        token = _normalize_token(header.text)
        if "service" in token and "service" not in index_map:
            index_map["service"] = idx
        elif "rate" in token and "rate" not in index_map:
            index_map["rate"] = idx
        elif "code" in token and "code" not in index_map:
            index_map["code"] = idx
    if not {"service", "rate", "code"}.issubset(index_map.keys()):
        return None
    return index_map


def _normalize_rate_value(raw: str) -> str:
    text = (raw or "").strip()
    text = text.replace(",", "")
    text = re.sub(r"[^0-9.\-]", "", text)
    return text


def _parse_rate_rows(table, service_type_label: str) -> List[Dict[str, str]]:
    index_map = _header_index_map(table)
    if not index_map:
        return []

    rows = table.find_elements(By.XPATH, ".//tbody/tr[td]")
    if not rows:
        rows = table.find_elements(By.XPATH, ".//tr[td]")

    parsed: List[Dict[str, str]] = []
    for row in rows:
        cells = row.find_elements(By.XPATH, "./td")
        if not cells:
            continue

        if max(index_map.values()) >= len(cells):
            continue

        service = (cells[index_map["service"]].text or "").strip()
        rate = _normalize_rate_value(cells[index_map["rate"]].text or "")
        line_item = (cells[index_map["code"]].text or "").strip()

        if _normalize_token(service) == "service" and _normalize_token(line_item) == "code":
            continue
        if not service and not rate and not line_item:
            continue

        parsed.append(
            {
                "service_type": service_type_label,
                "service": service,
                "rate": rate,
                "line_item_number": line_item,
            }
        )
    return parsed


def _table_signature(table) -> str:
    try:
        text = (table.text or "").strip()
    except Exception:
        text = ""
    return f"{len(text)}::{text[:4000]}"


def _wait_for_rate_table_ready(driver, previous_signature: str, timeout: int = 12):
    deadline = time.time() + timeout
    while time.time() < deadline:
        table = _find_rate_table(driver)
        if table is None:
            time.sleep(0.2)
            continue
        index_map = _header_index_map(table)
        if not index_map:
            time.sleep(0.2)
            continue
        signature = _table_signature(table)
        rows = _parse_rate_rows(table, "")
        if rows and (not previous_signature or signature != previous_signature):
            return table, signature
        time.sleep(0.25)
    raise TimeoutException("Rate table did not refresh with a new signature in time.")


def _find_visible_option_elements(driver):
    option_xpath = (
        "//*[@role='option']"
        " | //li[contains(@class,'select2-results__option')]"
        " | //ul[contains(@class,'options')]//li[normalize-space()]"
        " | //div[contains(@class,'option') and normalize-space()]"
    )
    options = []
    for option in driver.find_elements(By.XPATH, option_xpath):
        try:
            if not option.is_displayed():
                continue
        except Exception:
            continue
        text = (option.text or "").strip()
        if not text:
            continue
        options.append(option)
    return options


def _find_scrollable_parent(driver, element):
    script = """
    let node = arguments[0];
    while (node) {
      if (node.scrollHeight > node.clientHeight + 2) {
        return node;
      }
      node = node.parentElement;
    }
    return null;
    """
    try:
        return driver.execute_script(script, element)
    except Exception:
        return None


def _open_custom_dropdown(driver):
    trigger = _find_custom_service_type_trigger(driver)
    if trigger is None:
        raise RuntimeError("Service Type dropdown trigger not found.")
    click_js(driver, trigger)
    time.sleep(0.25)


def _close_custom_dropdown(driver):
    try:
        body = driver.find_element(By.TAG_NAME, "body")
        body.send_keys(Keys.ESCAPE)
    except Exception:
        pass


def _collect_custom_service_type_labels(driver, max_loops: int = 90) -> List[str]:
    _open_custom_dropdown(driver)
    labels: List[str] = []
    seen = set()
    stagnant_loops = 0
    last_count = 0
    scroll_box = None

    for _ in range(max_loops):
        options = _find_visible_option_elements(driver)
        if options and scroll_box is None:
            scroll_box = _find_scrollable_parent(driver, options[0])
        for option in options:
            text = (option.text or "").strip()
            if not text or _is_placeholder_label(text):
                continue
            if text in seen:
                continue
            seen.add(text)
            labels.append(text)

        if len(labels) == last_count:
            stagnant_loops += 1
        else:
            stagnant_loops = 0
            last_count = len(labels)

        moved = False
        if scroll_box is not None:
            try:
                moved = bool(
                    driver.execute_script(
                        "const el=arguments[0];"
                        "if(!el) return false;"
                        "const before=el.scrollTop;"
                        "el.scrollTop=Math.min(el.scrollTop+Math.max(120,Math.floor(el.clientHeight*0.8)), el.scrollHeight);"
                        "return el.scrollTop>before;",
                        scroll_box,
                    )
                )
            except Exception:
                moved = False

        if not moved and stagnant_loops >= 3:
            break
        time.sleep(0.2)

    _close_custom_dropdown(driver)
    return labels


def _collect_native_service_type_labels(driver) -> List[str]:
    select_elem = _find_native_service_type_select(driver)
    if select_elem is None:
        return []
    labels = []
    for option in Select(select_elem).options:
        text = (option.text or "").strip()
        if not text or _is_placeholder_label(text):
            continue
        labels.append(text)
    return labels


def _xpath_literal(text: str) -> str:
    if "'" not in text:
        return f"'{text}'"
    if '"' not in text:
        return f'"{text}"'
    parts = text.split("'")
    return "concat(" + ", \"'\", ".join([f"'{part}'" for part in parts]) + ")"


def _select_native_service_type(driver, label: str):
    select_elem = _find_native_service_type_select(driver)
    if select_elem is None:
        raise RuntimeError("Service Type <select> control not found.")
    Select(select_elem).select_by_visible_text(label)


def _select_custom_service_type(driver, label: str, max_loops: int = 90):
    _open_custom_dropdown(driver)
    scroll_box = None
    literal = _xpath_literal(label)

    for _ in range(max_loops):
        exact_xpath = (
            "//*[@role='option' and normalize-space()="
            + literal
            + "] | //li[contains(@class,'select2-results__option') and normalize-space()="
            + literal
            + "] | //div[contains(@class,'option') and normalize-space()="
            + literal
            + "]"
        )
        matches = driver.find_elements(By.XPATH, exact_xpath)
        option = _first_displayed(matches)
        if option is not None:
            click_js(driver, option)
            time.sleep(0.2)
            return

        options = _find_visible_option_elements(driver)
        if options and scroll_box is None:
            scroll_box = _find_scrollable_parent(driver, options[0])

        moved = False
        if scroll_box is not None:
            try:
                moved = bool(
                    driver.execute_script(
                        "const el=arguments[0];"
                        "if(!el) return false;"
                        "const before=el.scrollTop;"
                        "el.scrollTop=Math.min(el.scrollTop+Math.max(120,Math.floor(el.clientHeight*0.8)), el.scrollHeight);"
                        "return el.scrollTop>before;",
                        scroll_box,
                    )
                )
            except Exception:
                moved = False
        if not moved:
            break
        time.sleep(0.2)

    _close_custom_dropdown(driver)
    raise RuntimeError(f"Could not find Service Type option '{label}'.")


def _detect_service_type_mode(driver) -> str:
    if _find_native_service_type_select(driver) is not None:
        return "native"
    if _find_custom_service_type_trigger(driver) is not None:
        return "custom"
    return "unknown"


def _get_selected_service_type_label(driver, mode: str) -> str:
    if mode == "native":
        select_elem = _find_native_service_type_select(driver)
        if select_elem is None:
            return ""
        try:
            return (Select(select_elem).first_selected_option.text or "").strip()
        except Exception:
            return ""

    trigger = _find_custom_service_type_trigger(driver)
    if trigger is None:
        return ""
    candidates = [
        (trigger.text or "").strip(),
        (trigger.get_attribute("value") or "").strip(),
        (trigger.get_attribute("aria-label") or "").strip(),
    ]
    for candidate in candidates:
        if candidate and not _is_placeholder_label(candidate):
            return candidate
    return ""


def _collect_service_type_labels(driver, mode: str) -> List[str]:
    if mode == "native":
        labels = _collect_native_service_type_labels(driver)
    else:
        labels = _collect_custom_service_type_labels(driver)
    labels = [label for label in labels if not _is_placeholder_label(label)]
    return labels


def _select_service_type(driver, mode: str, label: str):
    if mode == "native":
        _select_native_service_type(driver, label)
    else:
        _select_custom_service_type(driver, label)


def _wait_for_service_type_control(driver, timeout: int = 20) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        mode = _detect_service_type_mode(driver)
        if mode in {"native", "custom"}:
            return mode
        time.sleep(0.25)
    raise RuntimeError("Service Type dropdown was not found on Add Appointment page.")


def _open_add_appointment_form(driver):
    add_btn = _find_add_appointment_button(driver)
    if add_btn is None:
        raise RuntimeError("Add Appointment button was not found on Appointments page.")

    handles_before = set(driver.window_handles)
    click_js(driver, add_btn)
    time.sleep(0.4)

    handles_after = set(driver.window_handles)
    new_handles = list(handles_after - handles_before)
    if new_handles:
        driver.switch_to.window(new_handles[-1])


def _navigate_to_add_appointment(driver, anchor_client_id: str, progress: ProgressCallback):
    _emit("ServiceType→Rate: navigating to dashboard.", progress)
    driver.get(TURNPOINT_DASHBOARD_URL)
    wait_for(driver, By.TAG_NAME, "body", timeout=15)

    _emit("ServiceType→Rate: opening clients list.", progress)
    driver.get(TURNPOINT_CLIENTS_URL)
    wait_for(driver, By.TAG_NAME, "body", timeout=15)

    details_url = TURNPOINT_CLIENT_DETAILS_URL_TMPL.format(client_id=anchor_client_id)
    _emit("ServiceType→Rate: opening anchor client details page.", progress)
    driver.get(details_url)
    wait_for(driver, By.TAG_NAME, "body", timeout=15)

    appts_url = TURNPOINT_CLIENT_APPOINTMENTS_URL_TMPL.format(client_id=anchor_client_id)
    _emit("ServiceType→Rate: opening appointments view.", progress)
    driver.get(appts_url)
    wait_for(driver, By.TAG_NAME, "body", timeout=15)

    _emit("ServiceType→Rate: opening Add Appointment form.", progress)
    _open_add_appointment_form(driver)
    wait_for(driver, By.TAG_NAME, "body", timeout=15)


def capture_live_rates(
    *,
    headless: bool = False,
    anchor_client_id: str = DEFAULT_ANCHOR_CLIENT_ID,
    on_row: RowCallback = None,
    on_progress: ProgressCallback = None,
    on_warning: ProgressCallback = None,
) -> List[Dict[str, str]]:
    """
    Extract canonical Service Type rate rows from TurnPoint Add Appointment form.
    This function is read-only and does not interact with purge counters/state.
    """

    ensure_credentials()
    TEMP_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    driver = build_chrome_driver(headless=headless, download_dir=TEMP_DOWNLOAD_DIR)
    extracted: List[Dict[str, str]] = []
    try:
        _emit("ServiceType→Rate: logging into TurnPoint.", on_progress)
        retry(lambda: login(driver), attempts=2, delay=0.8)

        _navigate_to_add_appointment(driver, str(anchor_client_id), on_progress)
        mode = _wait_for_service_type_control(driver, timeout=20)
        _emit(f"ServiceType→Rate: Service Type control detected ({mode}).", on_progress)

        labels = _collect_service_type_labels(driver, mode)
        if not labels:
            raise RuntimeError("No Service Type options were discovered.")

        selected_label = _get_selected_service_type_label(driver, mode)
        if selected_label in labels and len(labels) > 1:
            labels = [label for label in labels if label != selected_label] + [selected_label]

        _emit(f"ServiceType→Rate: discovered {len(labels)} Service Type option(s).", on_progress)

        pending_labels = list(labels)
        if _find_rate_table(driver) is None and pending_labels:
            # Hard guard: if the rate table cannot be discovered at all, abort early.
            probe_label = pending_labels[0]
            _emit(
                "ServiceType→Rate: probing rate table presence using first Service Type option.",
                on_progress,
            )
            _select_service_type(driver, mode, probe_label)
            try:
                probe_table, _ = _wait_for_rate_table_ready(driver, "", timeout=12)
            except Exception as exc:
                raise RuntimeError(
                    "Rate table with headers Service, Rate, and Code could not be found."
                ) from exc
            probe_rows = _parse_rate_rows(probe_table, probe_label)
            if not probe_rows:
                _emit(
                    f"ServiceType→Rate warning: '{probe_label}' returned zero rows.",
                    on_warning,
                )
            for row in probe_rows:
                extracted.append(row)
                if on_row:
                    try:
                        on_row(row)
                    except Exception:
                        pass
            pending_labels = pending_labels[1:]

        for index, service_type_label in enumerate(pending_labels, start=1):
            _emit(
                f"ServiceType→Rate: [{index}/{len(pending_labels)}] selecting '{service_type_label}'.",
                on_progress,
            )
            try:
                table = _find_rate_table(driver)
                previous_signature = _table_signature(table) if table is not None else ""

                _select_service_type(driver, mode, service_type_label)
                table, _ = _wait_for_rate_table_ready(driver, previous_signature, timeout=12)
                rows = _parse_rate_rows(table, service_type_label)
                if not rows:
                    warning_msg = (
                        f"ServiceType→Rate warning: '{service_type_label}' returned zero rows."
                    )
                    _emit(warning_msg, on_warning)
                    continue

                for row in rows:
                    extracted.append(row)
                    if on_row:
                        try:
                            on_row(row)
                        except Exception:
                            pass
            except Exception as exc:
                warning_msg = (
                    f"ServiceType→Rate warning: failed '{service_type_label}' ({exc}). Skipping."
                )
                _emit(warning_msg, on_warning)
                continue

        _emit(
            f"ServiceType→Rate: capture complete with {len(extracted)} row(s).",
            on_progress,
        )
        return extracted
    finally:
        driver.quit()

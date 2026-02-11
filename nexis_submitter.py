import os
import time
from typing import Dict

from selenium import webdriver  # type: ignore
from selenium.webdriver.common.by import By  # type: ignore
from selenium.webdriver.support.ui import WebDriverWait, Select  # type: ignore
from selenium.webdriver.support import expected_conditions as EC  # type: ignore

from selenium_helpers import click_js


BASE_URL = "https://nexis365.com/cohs"
LOGIN_URL = f"{BASE_URL}/login.php"
DASHBOARD_URL = f"{BASE_URL}/index.php"
EMPLOYEE_URL = f"{BASE_URL}/index.php?url=employees.php&id=59"


def build_driver(headless=False):
    chrome_options = webdriver.ChromeOptions()
    prefs = {
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
    }
    chrome_options.add_experimental_option("prefs", prefs)
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--window-size=1400,900")
    if headless:
        chrome_options.add_argument("--headless=new")
    return webdriver.Chrome(options=chrome_options)


def _find_first(driver, selectors):
    for by, locator in selectors:
        elems = driver.find_elements(by, locator)
        if elems:
            return elems[0]
    return None


def _wait_ready(driver, timeout=15):
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )


def _wait_overlay_clear(driver, timeout=15):
    """Wait until common loading overlays disappear."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            overlays = driver.find_elements(
                By.CSS_SELECTOR,
                "div[id*='load'],div[class*='load'],div[class*='overlay'],div[id*='overlay']",
            )
            visible = [o for o in overlays if o.is_displayed()]
            if not visible:
                return
        except Exception:
            return
        time.sleep(0.5)


def _inject_page_fixes(driver):
    """Patch broken scripts/overlays that block the employees page."""
    js = """
        try {
            if (typeof $ !== 'undefined' && $.fn && !$.fn.sortable) {
                $.fn.sortable = function(){ return this; };
            }
        } catch (e) {}
        try {
            document.querySelectorAll("div[id*='load'],div[class*='load'],div[class*='overlay'],div[id*='overlay']").forEach(el => el.remove());
            document.body.style.overflow = 'auto';
        } catch (e) {}
    """
    try:
        driver.execute_script(js)
    except Exception:
        return


def login(driver, username: str, password: str):
    driver.get(LOGIN_URL)
    _wait_ready(driver, timeout=15)
    user_field = _find_first(
        driver,
        [
            (By.NAME, "username"),
            (By.NAME, "email"),
            (By.NAME, "emailaddress"),
            (By.CSS_SELECTOR, "input[type='email']"),
            (By.CSS_SELECTOR, "input[type='text']"),
            (By.ID, "email"),
            (By.ID, "login_email"),
        ],
    )
    pass_field = _find_first(
        driver,
        [
            (By.NAME, "password"),
            (By.CSS_SELECTOR, "input[type='password']"),
            (By.ID, "password"),
            (By.ID, "login_password"),
        ],
    )
    submit = _find_first(
        driver,
        [
            (By.CSS_SELECTOR, "button[type='submit']"),
            (By.XPATH, "//button[contains(translate(.,'LOGIN','login'),'login')]"),
            (By.XPATH, "//input[@type='submit']"),
            (By.XPATH, "//button[contains(.,'Login') or contains(.,'Sign In')]"),
        ],
    )
    if not user_field or not pass_field or not submit:
        raise RuntimeError("Nexis login form fields not found.")
    user_field.clear()
    user_field.send_keys(username)
    pass_field.clear()
    pass_field.send_keys(password)
    click_js(driver, submit)
    try:
        WebDriverWait(driver, 20).until(
            EC.any_of(
                EC.url_contains("index.php"),
                EC.presence_of_element_located((By.XPATH, "//a[contains(.,'Logout') or contains(.,'Sign out') or contains(.,'sign out')]")),
            )
        )
    except Exception:
        raise RuntimeError(f"Nexis login did not redirect; current URL: {driver.current_url}")


def navigate_to_employee_form(driver):
    # Direct nav + overlay wait as the page is AJAX-loaded
    driver.get(EMPLOYEE_URL)
    _wait_ready(driver, timeout=20)
    _wait_overlay_clear(driver, timeout=15)
    _inject_page_fixes(driver)

    add_btn = _find_first(
        driver,
        [
            (By.XPATH, "//button[contains(.,'Add New User') or contains(.,'Add New Employee')]"),
            (By.XPATH, "//a[contains(.,'Add New User') or contains(.,'Add New Employee')]"),
            (By.CSS_SELECTOR, "button.btn.btn-outline-primary"),
            (By.XPATH, "//button[contains(@onclick,'employee') and contains(@onclick,'USER')]"),
        ],
    )
    if not add_btn:
        # try menu path as fallback
        driver.get(DASHBOARD_URL)
        _wait_ready(driver, timeout=15)
        _inject_page_fixes(driver)
        ndis_menu = _find_first(
            driver,
            [
                (By.XPATH, "//a[contains(.,'NDIS')]"),
                (By.XPATH, "//span[contains(.,'NDIS')]"),
            ],
        )
        if ndis_menu:
            click_js(driver, ndis_menu)
            time.sleep(0.5)
        user_account = _find_first(
            driver,
            [
                (By.XPATH, "//a[contains(.,'User Account')]"),
                (By.XPATH, "//a[contains(.,'User Accounts')]"),
            ],
        )
        if user_account:
            click_js(driver, user_account)
            time.sleep(0.5)
        employees_link = _find_first(
            driver,
            [
                (By.XPATH, "//a[contains(.,'Employees')]"),
                (By.XPATH, "//a[contains(@href,'employees.php')]"),
            ],
        )
        if employees_link:
            click_js(driver, employees_link)
            time.sleep(0.5)
        _wait_ready(driver, timeout=15)
        _wait_overlay_clear(driver, timeout=10)
        add_btn = _find_first(
            driver,
            [
                (By.XPATH, "//button[contains(.,'Add New User') or contains(.,'Add New Employee')]"),
                (By.XPATH, "//a[contains(.,'Add New User') or contains(.,'Add New Employee')]"),
                (By.CSS_SELECTOR, "button.btn.btn-outline-primary"),
                (By.XPATH, "//button[contains(@onclick,'employee') and contains(@onclick,'USER')]"),
            ],
        )
    if not add_btn:
        raise RuntimeError("Add New User/Employee button not found.")
    click_js(driver, add_btn)
    # If the button has a target (offcanvas), force-show it to bypass broken scripts
    try:
        target = add_btn.get_attribute("data-bs-target") or add_btn.get_attribute("data-target")
        if target and target.startswith("#"):
            driver.execute_script(
                "const el=document.querySelector(arguments[0]); if(el){el.classList.add('show'); el.style.display='block'; document.body.classList.add('offcanvas-open');}",
                target,
            )
    except Exception:
        pass
    _inject_page_fixes(driver)
    _wait_overlay_clear(driver, timeout=10)
    WebDriverWait(driver, 20).until(
        EC.presence_of_element_located((By.XPATH, "//form[contains(@id,'employee') or contains(@class,'employee') or contains(@action,'employee')]"))
    )


def _set_input_by_label(driver, label_text: str, value: str):
    if value is None:
        return
    xpath = (
        f"//label[contains(.,'{label_text}')]/following::*[self::input or self::textarea][1]"
        f" | //td[contains(.,'{label_text}')]/following::input[1]"
        f" | //td[contains(.,'{label_text}')]/following::textarea[1]"
        f" | //input[@name='{label_text}'] | //textarea[@name='{label_text}']"
    )
    try:
        elem = driver.find_element(By.XPATH, xpath)
        tag = elem.tag_name.lower()
        if tag == "select":
            try:
                Select(elem).select_by_visible_text(value)
            except Exception:
                pass
        else:
            elem.clear()
            elem.send_keys(value)
    except Exception:
        return


def _select_dropdown(driver, label_text: str, value: str):
    if value is None:
        return
    xpath = (
        f"//label[contains(.,'{label_text}')]/following::select[1]"
        f" | //td[contains(.,'{label_text}')]/following::select[1]"
        f" | //select[@name='{label_text}']"
    )
    try:
        elem = driver.find_element(By.XPATH, xpath)
        select = Select(elem)
        try:
            select.select_by_visible_text(value)
        except Exception:
            select.select_by_value(value)
    except Exception:
        return


def fill_employee_form(driver, payload: Dict[str, str]):
    # Personal detail
    _set_input_by_label(driver, "Posting Date", payload.get("PostingDate"))
    _set_input_by_label(driver, "Joining Date", payload.get("JoiningDate"))
    _select_dropdown(driver, "Title", payload.get("Title"))
    _set_input_by_label(driver, "First Name", payload.get("FirstName"))
    _set_input_by_label(driver, "Last Name", payload.get("LastName"))
    _set_input_by_label(driver, "Phone", payload.get("Phone"))
    _set_input_by_label(driver, "DOB", payload.get("DOB"))
    _set_input_by_label(driver, "Email Address", payload.get("Email"))
    _set_input_by_label(driver, "Login Password", payload.get("Password"))
    _select_dropdown(driver, "Gender", payload.get("Gender"))
    _set_input_by_label(driver, "Address", payload.get("Address"))
    _set_input_by_label(driver, "Address 2", payload.get("Address2", ""))
    _set_input_by_label(driver, "City", payload.get("City"))
    _set_input_by_label(driver, "State", payload.get("State"))
    _set_input_by_label(driver, "Zip", payload.get("Zip"))
    _set_input_by_label(driver, "Country", payload.get("Country"))

    # Official detail
    _set_input_by_label(driver, "Employee Card ID", payload.get("EmployeeCardID"))
    _select_dropdown(driver, "Designation", payload.get("Designation"))
    _select_dropdown(driver, "Department", payload.get("Department"))
    _set_input_by_label(driver, "Job Experience", payload.get("JobExperience"))
    _select_dropdown(driver, "Account Type", payload.get("AccountType"))
    _select_dropdown(driver, "Geographic Region", payload.get("Region"))

    # HR accounts
    _select_dropdown(driver, "SCHADS Status", payload.get("SCHADSStatus"))
    _select_dropdown(driver, "SCHADS Level", payload.get("SCHADSLevel"))
    _set_input_by_label(driver, "Pay Point", payload.get("PayPoint"))
    _select_dropdown(driver, "Wage Basic", payload.get("WageBasic"))
    _set_input_by_label(driver, "ABN", payload.get("ABN"))
    _select_dropdown(driver, "Payment Type", payload.get("PaymentType"))


def submit_employee(payload: Dict[str, str], *, headless=False, username=None, password=None):
    user = username or os.getenv("NEXIS_USERNAME")
    pwd = password or os.getenv("NEXIS_PASSWORD")
    if not user or not pwd:
        raise RuntimeError("NEXIS_USERNAME and NEXIS_PASSWORD must be set or passed in.")

    driver = build_driver(headless=headless)
    try:
        login(driver, user, pwd)
        navigate_to_employee_form(driver)
        fill_employee_form(driver, payload)
        # Try submit button
        submit_btn = driver.find_element(
            By.XPATH,
            "//button[contains(.,'Save') or contains(.,'Submit') or @type='submit']",
        )
        click_js(driver, submit_btn)
        time.sleep(2)
        if "employee" in driver.current_url.lower():
            # assume success if no error modal; otherwise let caller decide
            return
    finally:
        driver.quit()

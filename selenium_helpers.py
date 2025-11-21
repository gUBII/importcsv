import time
from typing import Callable, TypeVar

from selenium.webdriver.remote.webdriver import WebDriver  # type: ignore
from selenium.webdriver.support import expected_conditions as EC  # type: ignore
from selenium.webdriver.support.ui import WebDriverWait  # type: ignore
from selenium.webdriver.common.by import By  # type: ignore

T = TypeVar("T")


def retry(fn: Callable[[], T], attempts: int = 3, delay: float = 0.75) -> T:
    """Retry a callable with fixed delay; propagate the last exception on failure."""
    last_exc: Exception | None = None
    for _ in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(delay)
    if last_exc:
        raise last_exc
    raise RuntimeError("retry failed without capturing an exception")


def wait_for(driver: WebDriver, by: By, locator: str, timeout: int = 10):
    """Wait for presence of element located; returns the element."""
    return WebDriverWait(driver, timeout).until(EC.presence_of_element_located((by, locator)))


def click_js(driver: WebDriver, element):
    """Click using JS to bypass overlays."""
    driver.execute_script("arguments[0].click();", element)

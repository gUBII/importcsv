import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import service_type_rate_extractor as extractor  # noqa: E402


def test_default_rate_numeric_strips_currency_and_commas():
    assert extractor.default_rate_numeric("$1,234.50") == 1234.50
    assert extractor.default_rate_numeric(" 78.81 ") == 78.81
    assert extractor.default_rate_numeric("") == 0.0


def test_normalize_external_row_maps_core_fields():
    raw = {
        "Service Type": "(SIL) Active Night-Time",
        "ID": "12345",
        "Def. Rate": "$78.81",
        "Service Code": "01_803_0115_1_1",
        "Deleted": "No",
    }
    row = extractor.normalize_external_row(raw)

    assert row["Service Type"] == "(SIL) Active Night-Time"
    assert row["ID"] == "12345"
    assert row["Def. Rate"] == "$78.81"
    assert row["Service Code"] == "01_803_0115_1_1"
    assert row["Deleted"] == "No"
    assert row["ServiceTypeLink"].endswith("eid=12345")


def test_normalize_external_row_uses_link_to_backfill_id():
    raw = {
        "ServiceType": "Support Worker",
        "ServiceTypeLink": "https://tp1.com.au/service-type-details.asp?eid=99122",
    }
    row = extractor.normalize_external_row(raw)
    assert row["ID"] == "99122"


class _FakeElement:
    def __init__(self, text="", displayed=True):
        self.text = text
        self._displayed = displayed

    def is_displayed(self):
        return self._displayed


class _FakeDriver:
    def __init__(
        self,
        *,
        current_url="",
        body_text="",
        password_input_count=0,
        login_error_text="",
    ):
        self.current_url = current_url
        self._body = _FakeElement(text=body_text, displayed=True)
        self._password_inputs = [_FakeElement(displayed=True) for _ in range(password_input_count)]
        self._login_error = _FakeElement(text=login_error_text, displayed=True) if login_error_text else None

    def find_element(self, by, value):
        if by == extractor.By.TAG_NAME and value == "body":
            return self._body
        raise RuntimeError("unexpected find_element call")

    def find_elements(self, by, value):
        if by == extractor.By.XPATH and value == "//input[@type='password']":
            return list(self._password_inputs)
        if by == extractor.By.XPATH and value in {
            "//td[contains(@class,'red')]",
            "//*[contains(@class,'error')]",
            "//div[contains(@class,'error')]",
        }:
            return [self._login_error] if self._login_error else []
        return []


def test_password_change_notice_detected_from_banner_text():
    driver = _FakeDriver(
        current_url="https://tp1.com.au/dashboard.asp?welcome=yes",
        body_text="Password Change Required. Click here to change your password.",
        password_input_count=0,
    )
    assert extractor._is_password_change_required(driver) is True
    assert extractor._is_password_change_blocking(driver) is False


def test_password_change_is_blocking_when_reset_form_visible():
    driver = _FakeDriver(
        current_url="https://tp1.com.au/my-account.asp?tpMSG=no&eid=36269#top2",
        body_text="Please change your password now. New Password Confirm Password",
        password_input_count=3,
    )
    assert extractor._is_password_change_blocking(driver) is True


def test_login_failure_hint_detects_rejected_credentials():
    driver = _FakeDriver(
        current_url="https://tp1.com.au/login.asp",
        body_text="The Email and/or password you entered is incorrect. Please try again",
        login_error_text="The Email and/or password you entered is incorrect. Please try again",
    )
    assert extractor._login_failure_hint(driver) == "TurnPoint rejected credentials (email/password incorrect)."

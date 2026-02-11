import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import appointment_item_discovery as discovery  # noqa: E402


class _FakeDriver:
    def __init__(self, tmp_path: Path):
        self.current_url = "https://tp1.com.au/appointments.asp?posted=yes"
        self.title = "Appointments"
        self.page_source = "<html><body>appointment mock</body></html>"
        self._tmp_path = tmp_path
        self.body_text = "Appointment Details - New Service Type Add Appointment"

    def save_screenshot(self, path):
        Path(path).write_bytes(b"fake-png")
        return True

    def find_element(self, by, value):
        if by == discovery.By.TAG_NAME and value == "body":
            return type("Body", (), {"text": self.body_text})()
        raise RuntimeError(f"unexpected find_element {by}:{value}")


class _FakeOption:
    def __init__(self, text, attributes=None):
        self.text = text
        self._attributes = dict(attributes or {})

    def get_attribute(self, name):
        return self._attributes.get(name, "")


class _FakeDropdown:
    tag_name = "select"

    def __init__(self, options, *, visible=True, enabled=True):
        self.options = options
        self._visible = visible
        self._enabled = enabled

    def is_displayed(self):
        return self._visible

    def is_enabled(self):
        return self._enabled


class _FakeSelect:
    def __init__(self, element):
        self.options = list(getattr(element, "options", []))


class _FakeInput:
    def __init__(self, value=""):
        self.value = value
        self.sent = []

    def clear(self):
        self.value = ""

    def send_keys(self, chars):
        self.sent.append(chars)

    def get_attribute(self, name):
        if name == "value":
            return self.value
        return ""


def _read_checker_rows(path: Path):
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_checker_chain_success_path(monkeypatch, tmp_path):
    recorder = discovery.DiagnosticsRecorder(
        run_id="run_success",
        folder=tmp_path,
        on_event=None,
        on_progress=None,
    )
    driver = _FakeDriver(tmp_path)
    option = _FakeOption(
        text="Support Work - Weekday",
        attributes={
            "value": "service_type_id=777&item_number=01_111_0101_1_1&rate=55.2",
            "data-payload": '{"parent":"Support Work","item_number":"01_111_0101_1_1","rate":"55.2"}',
        },
    )
    dropdown = _FakeDropdown([option])

    monkeypatch.setattr(discovery, "Select", _FakeSelect)
    monkeypatch.setattr(
        discovery,
        "_find_service_type_dropdown",
        lambda _driver: (dropdown, "By.XPATH://select[@name='service_type']"),
    )

    _, _, payloads = discovery._inspect_dropdown(driver, recorder, "12345")

    assert len(payloads) == 1
    rows = _read_checker_rows(recorder.checkers_path)
    status_by_step = {(row["step"], row["code"]): row["status"] for row in rows}
    assert status_by_step[(discovery.CHECKER_DROPDOWN_PRESENT, discovery.CHECKER_DROPDOWN_PRESENT)] == "pass"
    assert status_by_step[(discovery.CHECKER_DROPDOWN_STATE, discovery.CHECKER_DROPDOWN_STATE)] == "pass"
    assert status_by_step[(discovery.CHECKER_DROPDOWN_OPTIONS, discovery.CHECKER_DROPDOWN_OPTIONS)] == "pass"
    assert status_by_step[(discovery.CHECKER_DROPDOWN_NONEMPTY, discovery.CHECKER_DROPDOWN_NONEMPTY)] == "pass"


def test_checker_dropdown_missing_captures_diagnostics(monkeypatch, tmp_path):
    recorder = discovery.DiagnosticsRecorder(
        run_id="run_missing",
        folder=tmp_path,
        on_event=None,
        on_progress=None,
    )
    driver = _FakeDriver(tmp_path)

    monkeypatch.setattr(
        discovery,
        "_find_service_type_dropdown",
        lambda _driver: (None, "By.XPATH://select[@name='missing']"),
    )

    dropdown, _, payloads = discovery._inspect_dropdown(driver, recorder, "12345")
    assert dropdown is None
    assert payloads == []

    rows = _read_checker_rows(recorder.checkers_path)
    fail_rows = [
        row
        for row in rows
        if row["step"] == discovery.CHECKER_DROPDOWN_PRESENT and row["status"] == "fail"
    ]
    assert len(fail_rows) == 1
    screenshot = Path(fail_rows[0]["artifact_screenshot"])
    html = Path(fail_rows[0]["artifact_html"])
    assert screenshot.exists()
    assert html.exists()


def test_checker_dropdown_empty_warns_and_continues(monkeypatch, tmp_path):
    recorder = discovery.DiagnosticsRecorder(
        run_id="run_empty",
        folder=tmp_path,
        on_event=None,
        on_progress=None,
    )
    driver = _FakeDriver(tmp_path)
    placeholder = _FakeOption(text="Select", attributes={"value": ""})
    dropdown = _FakeDropdown([placeholder])

    monkeypatch.setattr(discovery, "Select", _FakeSelect)
    monkeypatch.setattr(
        discovery,
        "_find_service_type_dropdown",
        lambda _driver: (dropdown, "By.XPATH://select[@name='service_type']"),
    )

    _, _, payloads = discovery._inspect_dropdown(driver, recorder, "77777")

    assert payloads == []
    rows = _read_checker_rows(recorder.checkers_path)
    warn_rows = [
        row
        for row in rows
        if row["step"] == discovery.CHECKER_DROPDOWN_NONEMPTY
        and row["code"] == discovery.CHECKER_DROPDOWN_EMPTY
        and row["status"] == "warn"
    ]
    assert len(warn_rows) == 1


def test_open_assist_appointments_new_success(monkeypatch, tmp_path):
    recorder = discovery.DiagnosticsRecorder(
        run_id="route_ok",
        folder=tmp_path,
        on_event=None,
        on_progress=None,
    )
    driver = _FakeDriver(tmp_path)
    wait_calls = {"count": 0}

    def fake_nav(_driver, url, timeout=20):
        _driver.current_url = url
        if "/appointments/new" in url:
            _driver.current_url = "https://assist.turnpoint.co/appointments/new"
            _driver.title = "New Appointment - Turnpoint Assist"
            _driver.body_text = "Appointment Details - New Service Type Add Appointment"
        elif "appointments-all.asp" in url:
            _driver.current_url = "https://assist.turnpoint.co/appointments?search=x"
            _driver.title = "Appointments - Turnpoint Assist"
            _driver.body_text = "Appointments list"
        return True

    def fake_wait(_predicate, timeout=30.0, interval=0.5):
        wait_calls["count"] += 1
        return True

    monkeypatch.setattr(discovery, "_navigate_and_wait_body", fake_nav)
    monkeypatch.setattr(discovery, "_wait_until", fake_wait)
    monkeypatch.setattr(discovery, "_is_404_page", lambda _driver: False)
    monkeypatch.setattr(discovery, "_is_auth_session_page", lambda _driver: False)

    ok = discovery._open_assist_appointments_new(driver, recorder)
    assert ok is True
    rows = _read_checker_rows(recorder.checkers_path)
    step_codes = {(row["step"], row["code"], row["status"]) for row in rows}
    assert (discovery.CHECKER_ROUTE_ASSIST, discovery.CHECKER_ROUTE_ASSIST, "pass") in step_codes
    assert (discovery.CHECKER_APPOINTMENT_NEW, discovery.CHECKER_APPOINTMENT_NEW, "pass") in step_codes
    assert wait_calls["count"] >= 2


def test_open_assist_appointments_new_stall(monkeypatch, tmp_path):
    recorder = discovery.DiagnosticsRecorder(
        run_id="route_stall",
        folder=tmp_path,
        on_event=None,
        on_progress=None,
    )
    driver = _FakeDriver(tmp_path)

    monkeypatch.setattr(discovery, "_navigate_and_wait_body", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(discovery, "_wait_until", lambda *_args, **_kwargs: False)

    ok = discovery._open_assist_appointments_new(driver, recorder)
    assert ok is False


def test_candidate_queries_prioritizes_probe_and_dedupes():
    values = discovery._candidate_queries("a", ["56851", "a", "", "a", "56851"])
    assert values == ["56851", "a", ""]


def test_inspect_assist_service_options_nonempty(monkeypatch, tmp_path):
    recorder = discovery.DiagnosticsRecorder(
        run_id="assist_opts",
        folder=tmp_path,
        on_event=None,
        on_progress=None,
    )
    driver = _FakeDriver(tmp_path)
    client_input = _FakeInput("")
    client_hidden = _FakeInput("110325")

    calls = []

    def fake_collect(
        _driver,
        *,
        container_id,
        seed_query,
        query_candidates=None,
        recorder,
        client_id,
        present_checker,
        options_checker,
        discovery_debug=False,
    ):
        calls.append(container_id)
        if container_id == "client_id":
            return client_input, client_hidden, [{"label": "A", "value": "110325"}]
        return _FakeInput(""), _FakeInput(""), [
            {"label": "Variant One", "value": "4701"},
            {"label": "Variant Two", "value": "7358"},
        ]

    monkeypatch.setattr(discovery, "_collect_assist_options", fake_collect)
    monkeypatch.setattr(discovery, "_select_first_assist_option", lambda *_args, **_kwargs: True)

    payloads = discovery._inspect_assist_service_options(driver, recorder, "56851", seed_query="a")
    assert len(payloads) == 2
    assert payloads[0]["text"] == "Variant One"
    assert payloads[0]["service_type_id"] == "4701"
    assert payloads[0]["value"] == "4701"
    assert calls == ["client_id", "service_type_id"]


def test_fetch_service_type_details_maps_ef581_ef592(monkeypatch, tmp_path):
    recorder = discovery.DiagnosticsRecorder(
        run_id="details_ok",
        folder=tmp_path,
        on_event=None,
        on_progress=None,
    )
    driver = _FakeDriver(tmp_path)

    class _Field:
        def __init__(self, value):
            self._value = value

        def get_attribute(self, name):
            if name == "value":
                return self._value
            return ""

    monkeypatch.setattr(discovery, "_navigate_and_wait_body", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(discovery, "_is_404_page", lambda _driver: False)

    def fake_find_element(by, value):
        if by == discovery.By.TAG_NAME and value == "body":
            return type("Body", (), {"text": "Service Type Details"})()
        if by == discovery.By.NAME and value == "ef581":
            return _Field("01_404_0104_1_1")
        if by == discovery.By.NAME and value == "ef592":
            return _Field("149.57")
        raise RuntimeError("unexpected field")

    driver.find_element = fake_find_element
    cache = {}
    details = discovery._fetch_service_type_details(
        driver,
        recorder,
        service_type_id="7358",
        source_client_id="56851",
        cache=cache,
    )
    assert details["item_number"] == "01_404_0104_1_1"
    assert details["rate"] == "149.57"
    assert details["ok"] == "1"


def test_parse_discovery_row_item_number_alias():
    payload = {
        "text": "Assistance With Self-Care Activities - Weekday Daytime",
        "value": "eid=5272",
        "data-service-code": "01_011_0107_1_1",
        "data-rate": "$67.28",
        "data-parent": "Assistance With Self-Care Activities",
    }
    row = discovery._parse_discovery_row(payload, source_client_id="56851")
    assert row["Item Number"] == "01_011_0107_1_1"
    assert row["Service Code"] == "01_011_0107_1_1"
    assert row["Rate"] == "67.28"
    assert row["Rate Source"] == "setter_payload"


def test_merge_exact_label_then_id_fallback():
    reference_rows = [
        {"Service Type": "Variant A", "ID": "111", "Def. Rate": "$10.00", "Service Code": ""},
        {"Service Type": "Variant B", "ID": "222", "Def. Rate": "$20.00", "Service Code": ""},
    ]
    discovered_rows = [
        {
            "Parent Service Type": "Parent A",
            "Service Variant Label": "Variant A",
            "Service Type ID": "999",
            "Item Number": "01_111_0001_1_1",
            "Service Code": "01_111_0001_1_1",
            "Rate": "55.50",
            "Rate Source": "setter_payload",
            "Unit Type": "Hourly",
            "Setter Value": "v1",
            "Payload JSON": "{}",
            "Source Client ID": "1001",
            "Captured At (UTC)": "2026-02-11T00:00:00+00:00",
        },
        {
            "Parent Service Type": "Parent B",
            "Service Variant Label": "Other Label",
            "Service Type ID": "222",
            "Item Number": "01_222_0002_1_1",
            "Service Code": "01_222_0002_1_1",
            "Rate": "",
            "Rate Source": "",
            "Unit Type": "Hourly",
            "Setter Value": "v2",
            "Payload JSON": "{}",
            "Source Client ID": "1002",
            "Captured At (UTC)": "2026-02-11T00:00:00+00:00",
        },
    ]

    merged = discovery.merge_discovery_with_service_types(reference_rows, discovered_rows)
    rows = merged["enriched_rows"]
    assert len(rows) == 2
    assert rows[0]["Item Number"] == "01_111_0001_1_1"
    assert rows[0]["Service Code"] == "01_111_0001_1_1"
    assert rows[0]["Rate"] == "55.50"
    assert rows[0]["Rate Source"] == "setter_payload"
    assert rows[1]["Item Number"] == "01_222_0002_1_1"
    assert rows[1]["Rate"] == "$20.00"
    assert rows[1]["Rate Source"] == "fallback_service_types"


def test_count_item_number_coverage():
    rows = [
        {"Item Number": "01_111_0001_1_1"},
        {"Item Number": ""},
        {"Item Number": "01_222_0002_1_1"},
    ]
    counts = discovery.count_item_number_coverage(rows)
    assert counts["rows_with_item_number"] == 2
    assert counts["rows_missing_item_number"] == 1

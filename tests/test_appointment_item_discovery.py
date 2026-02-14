"""Unit tests for appointment_item_discovery — Assist Service Type Variant Extractor."""

import csv
import json
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import appointment_item_discovery as discovery
import line_item_paths

# =============================================================================
# MOCK OBJECTS
# =============================================================================


class _FakeDriver:
    """Mock Selenium WebDriver."""

    def __init__(self, tmp_path: Path = None):
        self.current_url = "https://assist.turnpoint.co/appointments/new"
        self.title = "New Appointment - Turnpoint Assist"
        self.page_source = "<html><body>appointment form</body></html>"
        self._tmp_path = tmp_path
        self._elements = {}
        self.switch_to = _FakeSwitch(self)

    def quit(self):
        pass

    def get(self, url):
        self.current_url = url

    def find_element(self, by, value):
        """Mock find_element."""
        if (by, value) == ("tag name", "body"):
            return _FakeElement("appointment body")
        if (by, value) in self._elements:
            return self._elements[(by, value)]
        raise RuntimeError(f"Element not found: {by}:{value}")

    def find_elements(self, by, value):
        """Mock find_elements."""
        key = (by, value)
        if key in self._elements:
            elem = self._elements[key]
            if isinstance(elem, list):
                return elem
            return [elem]
        return []

    def execute_script(self, script, *args):
        """Mock execute_script."""
        if "/ hour" in script:
            return "/ hour"
        if "$" in script and args:
            prefix = args[0].get_attribute("data-cy").replace("_rate-input", "")
            return discovery.VARIANT_LABEL_FALLBACK.get(prefix, "")
        return None

    def save_screenshot(self, path):
        """Mock screenshot."""
        Path(path).write_bytes(b"fake-png")

    def get_log(self, _kind):
        return []

    def set_element(self, by, value, elem):
        """Set mock element for find_element."""
        self._elements[(by, value)] = elem


class _FakeSwitch:
    """Mock switch_to helper."""

    def __init__(self, driver):
        self.driver = driver
        self.last_frame = None

    def default_content(self):
        self.last_frame = None

    def frame(self, frame_elem):
        self.last_frame = frame_elem


class _FakeElement:
    """Mock web element."""

    def __init__(self, text="", tag_name="div", attributes=None):
        self.text = text
        self.tag_name = tag_name
        self._attributes = dict(attributes or {})
        self._children = []

    def get_attribute(self, name):
        return self._attributes.get(name, "")

    def click(self):
        pass

    def clear(self):
        pass

    def send_keys(self, keys):
        pass

    def find_element(self, by, value):
        for child in self._children:
            if hasattr(child, "text") and value in child.text:
                return child
        raise RuntimeError(f"Child element not found: {by}:{value}")

    def find_elements(self, by, value):
        return self._children


class _FakeInput(_FakeElement):
    """Mock input element."""

    def __init__(self, value="", attributes=None):
        super().__init__(text=value, tag_name="input", attributes=attributes)
        self._value = value
        self._sent_keys = []

    def get_attribute(self, name):
        if name == "value":
            return self._value
        return self._attributes.get(name, "")

    def clear(self):
        self._value = ""

    def send_keys(self, keys):
        self._sent_keys.append(keys)
        if keys not in ("\ue00d", "\ue007"):  # ESCAPE / ENTER
            self._value += str(keys)


# =============================================================================
# TEST FIXTURES
# =============================================================================


@pytest.fixture
def tmp_run_dir(tmp_path):
    """Create temporary directory structure for variant extraction."""
    # Create LineItemRates structure
    root = tmp_path / "LineItemRates"
    truth_root = root / "ServiceTypeTruth"
    truth_root.mkdir(parents=True, exist_ok=True)

    # Create variant directories
    (truth_root / "variants" / "latest").mkdir(parents=True, exist_ok=True)
    (truth_root / "variants" / "snapshots").mkdir(parents=True, exist_ok=True)
    (truth_root / "variants" / "diagnostics").mkdir(parents=True, exist_ok=True)
    (truth_root / "variants" / "checkpoints").mkdir(parents=True, exist_ok=True)

    return root


@pytest.fixture
def fake_driver():
    """Create fake driver."""
    return _FakeDriver()


# =============================================================================
# TESTS: TEXT NORMALIZATION & UTILITIES
# =============================================================================


def test_normalize_text():
    """Test text normalization."""
    assert discovery._normalize_text("  Hello   World  ") == "Hello World"
    assert discovery._normalize_text("") == ""
    assert discovery._normalize_text(None) == ""


def test_coerce_rate_text():
    """Test rate text coercion."""
    assert discovery._coerce_rate_text("$100.50") == "100.50"
    assert discovery._coerce_rate_text("100.50 per hour") == "100.50"
    assert discovery._coerce_rate_text("") == ""
    assert discovery._coerce_rate_text("no number") == "no number"


def test_sanitize_excel_text():
    """Test Excel text sanitization."""
    # Illegal chars should be removed
    text_with_illegal = "Hello\x00World\x08Test"
    cleaned, removed_count = discovery._sanitize_excel_text(text_with_illegal)
    assert removed_count == 2
    assert "\x00" not in cleaned


# =============================================================================
# TESTS: DIAGNOSTICS RECORDER
# =============================================================================


def test_diagnostics_recorder_save(tmp_path):
    """Test DiagnosticsRecorder saves events and checkers."""
    recorder = discovery.DiagnosticsRecorder("test_run", tmp_path)

    # Add event
    recorder.event("INFO", "collect_options", "TEST_EVENT", "Test message", option_count=5)

    # Add checker
    recorder.checker("open_assist", "PASS", "CHK_TEST", "Test passed")

    # Save
    recorder.save()

    # Verify files were created
    assert (tmp_path / "events.jsonl").exists()
    assert (tmp_path / "checkers.csv").exists()

    # Verify content
    with open(tmp_path / "events.jsonl") as f:
        events = [json.loads(line) for line in f]
    assert len(events) == 1
    assert events[0]["code"] == "TEST_EVENT"

    with open(tmp_path / "checkers.csv") as f:
        checkers = list(csv.DictReader(f))
    assert len(checkers) == 1
    assert checkers[0]["code"] == "CHK_TEST"


def test_diagnostics_recorder_save_writes_empty_checkers_file(tmp_path):
    """Recorder must emit checkers.csv even when no checker rows exist."""
    recorder = discovery.DiagnosticsRecorder("test_run", tmp_path)
    recorder.event("INFO", "collect_options", "TEST_EVENT", "Test message")
    recorder.save()
    assert (tmp_path / "checkers.csv").exists()
    with open(tmp_path / "checkers.csv") as f:
        checkers = list(csv.DictReader(f))
    assert checkers == []


# =============================================================================
# TESTS: VARIANT TABLE EXTRACTION
# =============================================================================


def test_extract_variant_table_rows_success(fake_driver, tmp_path):
    """Test extraction via the six fixed data-cy rate/code pairs."""
    recorder = discovery.DiagnosticsRecorder("test_run", tmp_path)

    expected = {
        "day": ("78.81", "01_803_0115_1_1"),
        "eve": ("78.81", "01_803_0115_1_1"),
        "night": ("78.81", "01_803_0115_1_1"),
        "saturday": ("98.83", "01_804_0115_1_1"),
        "sunday": ("127.43", "01_805_0115_1_1"),
        "ph": ("156.03", "01_806_0115_1_1"),
    }
    for prefix, (rate, code) in expected.items():
        fake_driver.set_element(
            "css selector",
            f"input[data-cy='{prefix}_rate-input']",
            [_FakeInput(value=rate, attributes={"data-cy": f"{prefix}_rate-input"})],
        )
        fake_driver.set_element(
            "css selector",
            f"input[data-cy='{prefix}_code-input']",
            [_FakeInput(value=code, attributes={"data-cy": f"{prefix}_code-input"})],
        )

    # Extract
    variants = discovery._extract_variant_table_rows(fake_driver, recorder)

    # Verify
    assert len(variants) == 6
    by_label = {row["Service Variant Label"]: row for row in variants}
    assert by_label["Weekday Daytime/Individual Code"]["Rate"] == "78.81"
    assert by_label["Weekday Daytime/Individual Code"]["Code"] == "01_803_0115_1_1"
    assert by_label["Saturday"]["Rate"] == "98.83"
    assert by_label["Sunday"]["Rate"] == "127.43"
    assert by_label["Public Holiday"]["Rate"] == "156.03"
    assert all(row["Unit"] == "/ hour" for row in variants)


def test_extract_variant_table_missing(fake_driver, tmp_path):
    """Test handling when required input pairs are missing."""
    recorder = discovery.DiagnosticsRecorder("test_run", tmp_path)

    # Missing fixed 6-pair inputs should fail extraction
    variants = discovery._extract_variant_table_rows(fake_driver, recorder)

    # Verify empty result and diagnostic
    assert variants == []
    recorder.save()
    with open(tmp_path / "events.jsonl") as f:
        events = [json.loads(line) for line in f]
    assert any("VARIANT_TABLE_MISSING" in e["code"] for e in events)


# =============================================================================
# TESTS: SERVICE TYPE SELECTION WITH RETRIES
# =============================================================================


def test_select_assist_option_full_label(fake_driver, tmp_path):
    """Test option selection using type+enter against primary XPath input."""
    recorder = discovery.DiagnosticsRecorder("test_run", tmp_path)

    input_elem = _FakeInput(attributes={"role": "combobox", "aria-expanded": "false"})
    fake_driver.set_element(
        "xpath",
        discovery.SERVICE_TYPE_INPUT_XPATH,
        [input_elem],
    )
    fake_driver.set_element("css selector", discovery.SERVICE_TYPE_LISTBOX_CSS, [_FakeElement("listbox")])

    # Try selection
    success = discovery._select_assist_option_with_retries(
        fake_driver,
        "7358",
        "Support Work",
        recorder,
    )

    assert success is True


def test_select_assist_option_all_fail(fake_driver, tmp_path):
    """Test when all selection strategies fail."""
    recorder = discovery.DiagnosticsRecorder("test_run", tmp_path)

    # No Service Type input available
    success = discovery._select_assist_option_with_retries(
        fake_driver,
        "7358",
        "Nonexistent Option",
        recorder,
    )

    # Should fail
    assert success is False

    # Verify error was recorded
    recorder.save()
    with open(tmp_path / "events.jsonl") as f:
        events = [json.loads(line) for line in f]
    assert any("SELECT_FAILED" in e["code"] for e in events)


def test_switch_to_variants_capable_editor_nested_iframes(fake_driver, tmp_path, monkeypatch):
    """Switch helper should navigate two-level iframe contract and pass capability gate."""
    recorder = discovery.DiagnosticsRecorder("test_run", tmp_path)
    outer_iframe = _FakeElement(attributes={"src": "https://tp1.com.au/appointment-edit.asp?NavShow=none"})
    inner_iframe = _FakeElement(attributes={"src": "https://assist.turnpoint.co/appointments/new?has_parent=true&client_id=92108&view_type=feature"})
    reload_anchor = _FakeElement(attributes={"data-cy": "service_type_id-reload"})
    service_input = _FakeInput(attributes={"role": "combobox", "aria-expanded": "false"})
    day_rate = _FakeInput(value="78.81", attributes={"data-cy": "day_rate-input"})
    day_code = _FakeInput(value="01_803_0115_1_1", attributes={"data-cy": "day_code-input"})

    fake_driver.set_element("css selector", discovery.OUTER_APPOINTMENT_IFRAME_CSS, [outer_iframe])
    fake_driver.set_element("css selector", discovery.INNER_ASSIST_IFRAME_CSS, [inner_iframe])
    fake_driver.set_element("css selector", discovery.SERVICE_TYPE_RELOAD_CSS, [reload_anchor])
    fake_driver.set_element("xpath", discovery.SERVICE_TYPE_INPUT_XPATH, [service_input])
    fake_driver.set_element("css selector", "input[data-cy='day_rate-input']", [day_rate])
    fake_driver.set_element("css selector", "input[data-cy='day_code-input']", [day_code])

    monkeypatch.setattr(discovery, "_click_add_appointment", lambda _driver: True)
    monkeypatch.setattr(discovery, "_select_service_type_option", lambda *args, **kwargs: True)

    class _Wait:
        def __init__(self, *_args, **_kwargs):
            pass
        def until(self, _cond):
            return True

    monkeypatch.setattr(discovery, "WebDriverWait", _Wait)
    ok = discovery._switch_to_variants_capable_editor(fake_driver, "92108", recorder)
    assert ok is True
    assert fake_driver.switch_to.last_frame == inner_iframe
    assert any(c["code"] == discovery.CHECKER_VARIANTS_EDITOR_ROUTE and c["status"] == "PASS" for c in recorder.checkers)


# =============================================================================
# TESTS: CHECKPOINT MANAGEMENT
# =============================================================================


def test_checkpoint_save_and_load(tmp_path, monkeypatch):
    """Test checkpoint saving and loading."""
    # Patch line_item_paths to use tmp_path
    def mock_get_variant_paths(run_id):
        return {
            "checkpoint_json": tmp_path / f"checkpoint_{run_id}.json",
            "checkpoint_append_csv": tmp_path / f"variants_append_{run_id}.csv",
            "diagnostics_dir": tmp_path / "diagnostics",
            "latest_csv": tmp_path / "latest.csv",
            "latest_xlsx": tmp_path / "latest.xlsx",
            "snapshot_csv": tmp_path / "snapshot.csv",
            "snapshot_xlsx": tmp_path / "snapshot.xlsx",
            "conflicts_csv": tmp_path / "conflicts.csv",
        }

    monkeypatch.setattr(line_item_paths, "get_variant_paths", mock_get_variant_paths)

    # Create and save checkpoint
    checkpoint = {
        "run_id": "test_run",
        "started_at": discovery._utc_now(),
        "probe_clients": ["12345"],
        "processed_service_type_ids": ["7358", "7359"],
        "failed_service_type_ids": ["7400"],
        "total_variant_rows": 100,
        "status": "in_progress",
    }
    discovery._update_checkpoint("test_run", checkpoint)

    # Load checkpoint
    loaded = discovery._load_checkpoint("test_run")

    # Verify
    assert loaded is not None
    assert loaded["run_id"] == "test_run"
    assert loaded["processed_service_type_ids"] == ["7358", "7359"]
    assert len(loaded["failed_service_type_ids"]) == 1


def test_checkpoint_resume_logic(tmp_path, monkeypatch):
    """Test checkpoint resume skips processed service types."""

    def mock_get_variant_paths(run_id):
        return {
            "checkpoint_json": tmp_path / f"checkpoint_{run_id}.json",
            "checkpoint_append_csv": tmp_path / f"variants_append_{run_id}.csv",
            "diagnostics_dir": tmp_path / "diagnostics",
            "latest_csv": tmp_path / "latest.csv",
            "latest_xlsx": tmp_path / "latest.xlsx",
            "snapshot_csv": tmp_path / "snapshot.csv",
            "snapshot_xlsx": tmp_path / "snapshot.xlsx",
            "conflicts_csv": tmp_path / "conflicts.csv",
        }

    monkeypatch.setattr(line_item_paths, "get_variant_paths", mock_get_variant_paths)

    # Create checkpoint with processed IDs
    checkpoint = {
        "run_id": "test_run",
        "started_at": discovery._utc_now(),
        "probe_clients": ["12345"],
        "processed_service_type_ids": ["7358", "7359"],
        "failed_service_type_ids": [],
        "total_variant_rows": 100,
        "status": "in_progress",
    }
    discovery._update_checkpoint("test_run", checkpoint)

    # Load checkpoint
    loaded = discovery._load_checkpoint("test_run")

    # Verify we can use it for skip logic
    processed_ids = set(loaded["processed_service_type_ids"])
    assert "7358" in processed_ids
    assert "7360" not in processed_ids


# =============================================================================
# TESTS: CONFLICT DETECTION
# =============================================================================


def test_conflict_detection_clean():
    """Test conflict detection for clean (non-conflicting) variants."""
    variants = [
        {
            "Parent Service Type ID": "7358",
            "Parent Service Type Label": "Support Work",
            "Service Variant Label": "Weekday Daytime",
            "Rate": "200.00",
            "Code": "01111",
            "Probe Client ID": "12345",
        },
        {
            "Parent Service Type ID": "7358",
            "Parent Service Type Label": "Support Work",
            "Service Variant Label": "Weekday Evening",
            "Rate": "250.00",
            "Code": "01112",
            "Probe Client ID": "12345",
        },
    ]

    clean, conflicts = discovery._detect_conflicts(variants)

    # Should be clean (same parent, different variants)
    assert len(clean) == 2
    assert len(conflicts) == 0
    assert all(v["Conflict"] == "NO" for v in clean)


def test_conflict_detection_conflicts():
    """Test conflict detection identifies conflicting variants."""
    variants = [
        {
            "Parent Service Type ID": "7358",
            "Parent Service Type Label": "Support Work",
            "Service Variant Label": "Weekday Daytime",
            "Rate": "200.00",
            "Code": "01111",
            "Probe Client ID": "12345",
        },
        {
            "Parent Service Type ID": "7358",
            "Parent Service Type Label": "Support Work",
            "Service Variant Label": "Weekday Daytime",
            "Rate": "210.00",  # DIFFERENT RATE
            "Code": "01111",
            "Probe Client ID": "67890",
        },
    ]

    clean, conflicts = discovery._detect_conflicts(variants)

    # Should detect conflict
    assert len(clean) == 0
    assert len(conflicts) == 2
    assert all(v["Conflict"] == "YES" for v in conflicts)
    assert any("rates=" in v.get("Conflict Detail", "") for v in conflicts)


# =============================================================================
# TESTS: FILE I/O
# =============================================================================


def test_write_csv(tmp_path):
    """Test CSV writing."""
    rows = [
        {
            "Parent Service Type ID": "7358",
            "Parent Service Type Label": "Support Work",
            "Service Variant Label": "Weekday Daytime",
            "Rate": "200.00",
            "Rate (Raw)": "$200.00",
            "Code": "01111",
            "Code (Raw)": "01_111",
            "Unit": "per hour",
            "Conflict": "NO",
            "Probe Client ID": "12345",
            "Source URL": "https://assist.turnpoint.co/appointments/new",
            "Captured At (UTC)": discovery._utc_now(),
        }
    ]

    output_path = tmp_path / "test.csv"
    discovery._write_csv(rows, discovery.VARIANT_COLUMNS, output_path)

    # Verify file exists and content
    assert output_path.exists()
    with open(output_path) as f:
        reader = csv.DictReader(f)
        data = list(reader)
    assert len(data) == 1
    assert data[0]["Parent Service Type ID"] == "7358"


def test_write_xlsx(tmp_path):
    """Test XLSX writing."""
    rows = [
        {"Parent Service Type ID": "7358", "Service Variant Label": "Variant 1"},
        {"Parent Service Type ID": "7358", "Service Variant Label": "Variant 2"},
    ]

    # Add all columns with empty values for simplicity
    full_rows = []
    for row in rows:
        full_row = {col: row.get(col, "") for col in discovery.VARIANT_COLUMNS}
        full_rows.append(full_row)

    output_path = tmp_path / "test.xlsx"
    discovery._write_xlsx(full_rows, discovery.VARIANT_COLUMNS, output_path)

    # Verify file exists
    assert output_path.exists()
    assert output_path.stat().st_size > 0


def test_xlsx_merged_cells(tmp_path):
    """Test XLSX merged cells formatting."""
    rows = [
        {col: "" for col in discovery.VARIANT_COLUMNS},
        {col: "" for col in discovery.VARIANT_COLUMNS},
    ]
    rows[0]["Parent Service Type ID"] = "7358"
    rows[1]["Parent Service Type ID"] = "7358"

    output_path = tmp_path / "test.xlsx"
    discovery._write_xlsx(rows, discovery.VARIANT_COLUMNS, output_path)
    discovery._apply_xlsx_merged_cells(output_path)

    # Verify file exists and merged cells were applied
    assert output_path.exists()
    from openpyxl import load_workbook

    wb = load_workbook(output_path)
    ws = wb.active
    # Check that freeze panes was set
    assert ws.freeze_panes is not None


# =============================================================================
# TESTS: APPEND CHECKPOINT CSV
# =============================================================================


def test_append_checkpoint_csv(tmp_path, monkeypatch):
    """Test appending to checkpoint CSV."""

    def mock_get_variant_paths(run_id):
        return {
            "checkpoint_append_csv": tmp_path / f"variants_append_{run_id}.csv",
        }

    monkeypatch.setattr(line_item_paths, "get_variant_paths", mock_get_variant_paths)

    # Create simple test rows
    rows = [
        {col: "test_value_0" for col in discovery.VARIANT_COLUMNS}
        for _ in range(1)
    ]

    # First append
    discovery._append_to_checkpoint_csv("test_run", rows)

    # Verify file exists
    csv_path = tmp_path / "variants_append_test_run.csv"
    assert csv_path.exists()

    # Append again
    discovery._append_to_checkpoint_csv("test_run", rows)

    # Verify rows were appended (not duplicated header)
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        data = list(reader)
    assert len(data) == 2  # Two appends


# =============================================================================
# INTEGRATION TEST
# =============================================================================


def test_variant_column_schema():
    """Test that variant column schema is correct."""
    expected_columns = [
        "Parent Service Type ID",
        "Parent Service Type Label",
        "Parent Service Type",
        "Service Type ID",
        "Service Variant Label",
        "Rate",
        "Rate (Raw)",
        "Code",
        "Code (Raw)",
        "Item Number",
        "Unit",
        "Status",
        "Error Reason",
        "Conflict",
        "Conflict Detail",
        "Probe Client ID",
        "Source URL",
        "Captured At (UTC)",
    ]
    assert discovery.VARIANT_COLUMNS == expected_columns


def test_build_variant_record_includes_truthview_alias_fields():
    """Variant output rows include alias fields used by TruthView import."""
    row = discovery._build_variant_record(
        service_type_id="7358",
        service_type_label="(SIL) Active Night-Time",
        variant={
            "Service Variant Label": "Weekday Evening",
            "Rate": "78.81",
            "Rate (Raw)": "78.81",
            "Code": "01_803_0115_1_1",
            "Code (Raw)": "01_803_0115_1_1",
            "Unit": "/ hour",
        },
        probe_client_id="92108",
        source_url="https://assist.turnpoint.co/appointments/new?has_parent=true",
    )
    assert row["Parent Service Type"] == row["Parent Service Type Label"]
    assert row["Service Type ID"] == row["Parent Service Type ID"]
    assert row["Item Number"] == row["Code"]


def test_discover_appointment_item_numbers_forwards_smoke_kwargs(monkeypatch):
    """Legacy wrapper should forward smoke-mode parameters."""
    captured = {}

    def _fake_extract(**kwargs):
        captured.update(kwargs)
        return {"output_paths": {}, "total_variant_rows": 0}

    monkeypatch.setattr(discovery, "extract_service_type_variants", _fake_extract)
    discovery.discover_appointment_item_numbers(
        probe_client_id="92108",
        smoke_mode=True,
        smoke_service_type_id="7358",
        smoke_service_type_label="Night",
    )

    assert captured["probe_client_ids"] == "92108"
    assert captured["smoke_mode"] is True
    assert captured["smoke_service_type_id"] == "7358"
    assert captured["smoke_service_type_label"] == "Night"

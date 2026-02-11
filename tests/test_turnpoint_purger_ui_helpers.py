import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import turnpoint_purger_ui as ui  # noqa: E402


def test_rate_table_columns_for_metadata_mode():
    columns = ui.rate_table_columns_for_mode(ui.RATE_MODE_METADATA)
    ids = [item["id"] for item in columns]
    assert ids == [
        "service_type",
        "service_type_id",
        "default_rate",
        "service_code",
        "service_type_link",
    ]


def test_rate_table_columns_for_discovery_mode():
    columns = ui.rate_table_columns_for_mode(ui.RATE_MODE_DISCOVERY)
    ids = [item["id"] for item in columns]
    assert ids == ui.DISCOVERY_TABLE_FIELDS


def test_normalize_discovery_row_for_table():
    row = {
        "Parent Service Type": "Support",
        "Service Variant Label": "Weekday Day",
        "Service Type ID": 7358,
        "Item Number": "01_404_0104_1_1",
    }
    normalized = ui.normalize_discovery_row_for_table(row)
    assert normalized["Parent Service Type"] == "Support"
    assert normalized["Service Variant Label"] == "Weekday Day"
    assert normalized["Service Type ID"] == "7358"
    assert normalized["Item Number"] == "01_404_0104_1_1"
    assert normalized["Rate Source"] == ""


def test_discovery_row_matches_search():
    row = {
        "Parent Service Type": "Assistance",
        "Service Variant Label": "Weekday Night",
        "Service Type ID": "7358",
        "Item Number": "01_404_0104_1_1",
        "Service Code": "01_404_0104_1_1",
        "Rate": "149.57",
        "Rate Source": "service_type_details",
        "Source Client ID": "56851",
        "Captured At (UTC)": "2026-02-11T00:00:00+00:00",
    }
    assert ui.discovery_row_matches_search(row, "weekday")
    assert ui.discovery_row_matches_search(row, "149.57")
    assert ui.discovery_row_matches_search(row, "56851")
    assert not ui.discovery_row_matches_search(row, "not-present")

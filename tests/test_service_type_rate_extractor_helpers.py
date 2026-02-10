import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import service_type_rate_extractor as extractor


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
    row = extractor.normalize_external_row(raw, captured_at="2026-01-01T00:00:00+00:00")

    assert row["ServiceType"] == "(SIL) Active Night-Time"
    assert row["ServiceTypeID"] == "12345"
    assert row["DefaultRate"] == "$78.81"
    assert row["ServiceCode"] == "01_803_0115_1_1"
    assert row["Deleted"] == "No"
    assert row["ServiceTypeLink"].endswith("eid=12345")
    assert row["SourcePage"] == extractor.SERVICE_TYPES_PAGE_URL


def test_normalize_external_row_uses_link_to_backfill_id():
    raw = {
        "ServiceType": "Support Worker",
        "ServiceTypeLink": "https://tp1.com.au/service-type-details.asp?eid=99122",
    }
    row = extractor.normalize_external_row(raw, captured_at="2026-01-01T00:00:00+00:00")
    assert row["ServiceTypeID"] == "99122"

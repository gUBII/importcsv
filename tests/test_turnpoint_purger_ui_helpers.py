import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from truth_store import TruthStore  # noqa: E402


def test_truth_store_upsert_reference():
    store = TruthStore()
    store.upsert_reference({
        "ID": "7358",
        "Service Type": "Weekday Day",
        "Def. Rate": "149.57",
        "Service Code": "01_404_0104_1_1",
    })
    rec = store.get_record("7358")
    assert rec is not None
    assert rec.service_type_id == "7358"
    assert rec.truth_rate == "149.57"
    assert rec.truth_rate_source == "reference"
    assert rec.service_variant_label == "Weekday Day"
    assert rec.status == "blue"  # has both rate and item


def test_truth_store_upsert_discovery():
    store = TruthStore()
    store.upsert_discovery({
        "Service Type ID": "7358",
        "Parent Service Type": "Assistance",
        "Service Variant Label": "Weekday Day",
        "Rate": "149.57",
        "Item Number": "01_404_0104_1_1",
    })
    rec = store.get_record("7358")
    assert rec is not None
    assert rec.parent_service_type == "Assistance"
    assert rec.service_variant_label == "Weekday Day"
    assert rec.truth_rate == "149.57"
    assert rec.truth_item_number == "01_404_0104_1_1"
    assert rec.status == "blue"


def test_truth_store_upsert_discovery_prefers_service_variant_id():
    store = TruthStore()
    store.upsert_discovery({
        "Service Type ID": "7358",
        "Service Variant ID": "7358::day",
        "Service Variant Prefix": "day",
        "Parent Service Type": "Assistance",
        "Service Variant Label": "Weekday Day",
        "Rate": "149.57",
        "Item Number": "01_404_0104_1_1",
    })
    rec = store.get_record("7358::day")
    assert rec is not None
    assert rec.service_type_id == "7358::day"
    assert rec.service_variant_prefix == "day"
    assert rec.parent_service_type == "Assistance"
    assert rec.service_variant_label == "Weekday Day"


def test_truth_store_conflict_detection():
    store = TruthStore()
    store.upsert_reference({
        "ID": "7358",
        "Service Type": "Weekday Day",
        "Def. Rate": "149.57",
    })
    store.upsert_discovery({
        "Service Type ID": "7358",
        "Rate": "155.00",
    })
    rec = store.get_record("7358")
    assert rec is not None
    assert rec.rate_conflict is True
    # Discovery takes precedence over reference
    assert rec.truth_rate == "155.00"
    assert rec.truth_rate_source == "discovery"


def test_truth_store_status_counts():
    store = TruthStore()
    # RED: neither rate nor item
    store.upsert_discovery({
        "Service Type ID": "1000",
        "Parent Service Type": "Group A",
        "Service Variant Label": "No Data",
    })
    # YELLOW: rate only
    store.upsert_discovery({
        "Service Type ID": "2000",
        "Parent Service Type": "Group A",
        "Service Variant Label": "Rate Only",
        "Rate": "50.00",
    })
    # BLUE: both
    store.upsert_discovery({
        "Service Type ID": "3000",
        "Parent Service Type": "Group B",
        "Service Variant Label": "Both",
        "Rate": "75.00",
        "Item Number": "01_234",
    })
    counts = store.get_status_counts()
    assert counts["red"] == 1
    assert counts["yellow"] == 1
    assert counts["blue"] == 1


def test_discovery_error_dialog_message_is_concise():
    """UI should show short discovery failure dialogs (not raw stack dumps)."""
    source = (ROOT / "turnpoint_purger_ui.py").read_text(encoding="utf-8")
    assert "Full details were saved in diagnostics." in source
    assert "Use 'Open Diagnostics Folder'." in source

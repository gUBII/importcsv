import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import importcsv  # noqa: E402


class FakeElement:
    def __init__(self, href, text):
        self._href = href
        self.text = text

    def get_attribute(self, name):
        if name == "href":
            return self._href
        return ""


def test_extract_client_rows_from_elements():
    elements = [
        FakeElement("https://tp1.com.au/client-details.asp?eid=12345", "Client A"),
        FakeElement("https://tp1.com.au/client-details.asp?eid=67890", "Client B"),
        FakeElement("https://tp1.com.au/client-details.asp?foo=bar", "Invalid"),
    ]
    rows = importcsv._extract_client_rows_from_elements(elements, "NDIS - Plan Managed")
    assert len(rows) == 2
    assert rows[0]["client_id"] == "12345"
    assert rows[0]["client_name"] == "Client A"
    assert rows[0]["package"] == "NDIS - Plan Managed"
    assert rows[1]["client_id"] == "67890"


def test_write_package_manifest(tmp_path):
    entries = [
        {
            "package": "NDIS - Plan Managed",
            "client_id": "12345",
            "client_name": "Client A",
            "details_url": "https://tp1.com.au/client-details.asp?eid=12345",
        },
        {
            "package": "NDIS - NDIA Managed",
            "client_id": "67890",
            "client_name": "Client B",
            "details_url": "https://tp1.com.au/client-details.asp?eid=67890",
        },
    ]
    manifest_path = tmp_path / "manifest.csv"
    importcsv._write_package_manifest(entries, manifest_path)
    assert manifest_path.exists()
    with manifest_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
    assert len(rows) == 2
    assert rows[0]["Order"] == "1"
    assert rows[0]["Client ID"] == "12345"
    assert rows[1]["Order"] == "2"

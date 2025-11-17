import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import importcsv


def test_resolve_purgeable_clients_url_prefers_override(monkeypatch):
    monkeypatch.setattr(importcsv, "PURGEABLE_CLIENTS_URL", "https://env.example.com/list.asp")
    result = importcsv.resolve_purgeable_clients_url()
    assert result == "https://env.example.com/list.asp"

    override = "https://override.example.com/list.asp"
    result_override = importcsv.resolve_purgeable_clients_url(override)
    assert result_override == override

    monkeypatch.setattr(importcsv, "PURGEABLE_CLIENTS_URL", None)
    defaulted = importcsv.resolve_purgeable_clients_url()
    assert defaulted == importcsv.DEFAULT_PURGEABLE_CLIENTS_URL


def test_assert_valid_purgeable_page_allows_normal_text():
    class FakeBody:
        text = "Client list loaded successfully"

    class FakeDriver:
        def find_element(self, *_args, **_kwargs):
            return FakeBody()

    importcsv._assert_valid_purgeable_page(FakeDriver(), "https://example.com/clients")


def test_assert_valid_purgeable_page_raises_on_404():
    class FakeBody:
        text = "HTTP Error 404 - Not Found 0x80070002"

    class FakeDriver:
        def find_element(self, *_args, **_kwargs):
            return FakeBody()

    with pytest.raises(RuntimeError):
        importcsv._assert_valid_purgeable_page(
            FakeDriver(), "https://tp1.com.au/missing.asp"
        )

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import service_type_rate_extractor as rate_extractor


def test_normalize_rate_value_strips_currency_and_commas():
    assert rate_extractor._normalize_rate_value("$1,234.50") == "1234.50"
    assert rate_extractor._normalize_rate_value(" 78.81 ") == "78.81"


def test_placeholder_label_detection():
    assert rate_extractor._is_placeholder_label("-")
    assert rate_extractor._is_placeholder_label("Select...")
    assert not rate_extractor._is_placeholder_label("Weekday Night")


def test_xpath_literal_handles_quotes():
    text = "Worker's \"Special\" Service"
    literal = rate_extractor._xpath_literal(text)
    assert literal.startswith("concat(")

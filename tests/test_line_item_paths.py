"""Unit tests for line_item_paths — path generation and run ID helpers."""

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import line_item_paths  # noqa: E402


# ---------------------------------------------------------------------------
# make_run_id
# ---------------------------------------------------------------------------

def test_make_run_id_format():
    """Run ID follows YYYYMMDD_HHMMSS_TAG pattern."""
    rid = line_item_paths.make_run_id("CAPTURE")
    assert re.fullmatch(r"\d{8}_\d{6}_[A-Za-z0-9_]{1,12}", rid), f"Unexpected format: {rid}"


def test_make_run_id_default_tag():
    """Default tag is AUTO when not specified."""
    rid = line_item_paths.make_run_id()
    assert rid.endswith("_AUTO"), f"Expected AUTO tag, got: {rid}"


def test_make_run_id_sanitizes_tag():
    """Special characters are stripped and tag is capped at 12 chars."""
    rid = line_item_paths.make_run_id("Hello World!@#$%^&*()")
    tag = rid.split("_", 2)[2]
    assert re.fullmatch(r"[A-Za-z0-9_]+", tag), f"Tag contains disallowed chars: {tag}"
    assert len(tag) <= 12, f"Tag exceeds 12 chars: {tag}"


def test_make_run_id_empty_tag_fallback():
    """Empty or None tag falls back to AUTO."""
    rid = line_item_paths.make_run_id("")
    assert rid.endswith("_AUTO"), f"Expected AUTO fallback, got: {rid}"


# ---------------------------------------------------------------------------
# get_root
# ---------------------------------------------------------------------------

def test_get_root_default():
    """Default root resolves to ~/LineItemRates."""
    root = line_item_paths.get_root()
    assert root.name == "LineItemRates"
    assert root.is_absolute()


# ---------------------------------------------------------------------------
# Reference paths
# ---------------------------------------------------------------------------

def test_reference_paths():
    """get_reference_paths returns expected keys and filenames."""
    paths = line_item_paths.get_reference_paths("20250101_120000_TEST")
    assert "snapshot_csv" in paths
    assert "snapshot_xlsx" in paths
    assert "latest_csv" in paths
    assert "latest_xlsx" in paths

    assert paths["snapshot_csv"].name == "ServiceTypes_reference_20250101_120000_TEST.csv"
    assert paths["snapshot_xlsx"].name == "ServiceTypes_reference_20250101_120000_TEST.xlsx"
    assert paths["latest_csv"].name == "ServiceTypes_latest.csv"
    assert paths["latest_xlsx"].name == "ServiceTypes_latest.xlsx"

    assert "reference" in str(paths["snapshot_csv"])
    assert "snapshots" in str(paths["snapshot_csv"])


# ---------------------------------------------------------------------------
# Discovery paths
# ---------------------------------------------------------------------------

def test_discovery_paths():
    """get_discovery_paths returns expected keys and filenames."""
    paths = line_item_paths.get_discovery_paths("20250101_120000_DISC")
    assert "snapshot_csv" in paths
    assert "snapshot_xlsx" in paths
    assert "latest_csv" in paths
    assert "latest_xlsx" in paths
    assert "diagnostics_dir" in paths

    assert paths["snapshot_csv"].name == "AppointmentItemDiscovery_20250101_120000_DISC.csv"
    assert paths["latest_csv"].name == "AppointmentItemDiscovery_latest.csv"
    assert paths["diagnostics_dir"].name == "20250101_120000_DISC"


# ---------------------------------------------------------------------------
# Enriched paths
# ---------------------------------------------------------------------------

def test_enriched_paths():
    """get_enriched_paths returns expected keys and filenames."""
    paths = line_item_paths.get_enriched_paths("20250101_120000_MERGE")
    assert "enriched_snapshot_csv" in paths
    assert "enriched_snapshot_xlsx" in paths
    assert "enriched_latest_csv" in paths
    assert "enriched_latest_xlsx" in paths
    assert "unmatched_snapshot_csv" in paths
    assert "unmatched_latest_csv" in paths

    assert paths["enriched_snapshot_csv"].name == "ServiceTypes_enriched_20250101_120000_MERGE.csv"
    assert paths["enriched_latest_csv"].name == "ServiceTypes_enriched_latest.csv"
    assert paths["unmatched_snapshot_csv"].name == "ServiceTypes_unmatched_discovery_20250101_120000_MERGE.csv"
    assert paths["unmatched_latest_csv"].name == "ServiceTypes_unmatched_discovery_latest.csv"


# ---------------------------------------------------------------------------
# Export path
# ---------------------------------------------------------------------------

def test_export_path_csv():
    """get_export_path returns correct CSV export path."""
    p = line_item_paths.get_export_path("20250101_120000_EXP", "csv")
    assert p.name == "TruthView_export_20250101_120000_EXP.csv"
    assert "exports" in str(p)
    assert "manual" in str(p)


def test_export_path_xlsx():
    """get_export_path returns correct XLSX export path."""
    p = line_item_paths.get_export_path("20250101_120000_EXP", "xlsx")
    assert p.name == "TruthView_export_20250101_120000_EXP.xlsx"


def test_export_path_invalid_format_defaults_csv():
    """get_export_path defaults to csv for unknown formats."""
    p = line_item_paths.get_export_path("RUN1", "pdf")
    assert p.suffix == ".csv"


# ---------------------------------------------------------------------------
# Well-known latest paths
# ---------------------------------------------------------------------------

def test_well_known_latest_paths():
    """Convenience functions return correct filenames."""
    assert line_item_paths.reference_latest_csv().name == "ServiceTypes_latest.csv"
    assert line_item_paths.reference_latest_xlsx().name == "ServiceTypes_latest.xlsx"
    assert line_item_paths.discovery_latest_csv().name == "AppointmentItemDiscovery_latest.csv"
    assert line_item_paths.discovery_latest_xlsx().name == "AppointmentItemDiscovery_latest.xlsx"


# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------

def test_truth_root():
    """get_truth_root returns ServiceTypeTruth under the root."""
    truth = line_item_paths.get_truth_root()
    assert truth.name == "ServiceTypeTruth"
    assert truth.parent == line_item_paths.get_root()


def test_downloads_dir():
    """downloads_dir is under ServiceTypeTruth/_downloads."""
    d = line_item_paths.downloads_dir()
    assert d.name == "_downloads"
    assert "ServiceTypeTruth" in str(d)


def test_cleanup_logs_dir():
    """cleanup_logs_dir is under ServiceTypeTruth/_cleanup_logs."""
    d = line_item_paths.cleanup_logs_dir()
    assert d.name == "_cleanup_logs"
    assert "ServiceTypeTruth" in str(d)

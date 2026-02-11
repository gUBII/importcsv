"""
LineItemRates path builder — single source of truth for all
service-type discovery / rate output paths.

Default root:  ~/LineItemRates
Override:      LINE_ITEM_RATES_ROOT=/custom/path
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------

_ROOT = Path(
    os.getenv("LINE_ITEM_RATES_ROOT", str(Path.home() / "LineItemRates"))
).expanduser().resolve()

_TRUTH = "ServiceTypeTruth"


def get_root() -> Path:
    """Return the resolved LineItemRates root directory."""
    return _ROOT


# ---------------------------------------------------------------------------
# Category paths
# ---------------------------------------------------------------------------

def _cat(*parts: str) -> Path:
    return _ROOT / _TRUTH / Path(*parts)


def reference_latest_dir() -> Path:
    return _cat("reference", "latest")


def reference_snapshots_dir() -> Path:
    return _cat("reference", "snapshots")


def discovery_latest_dir() -> Path:
    return _cat("discovery", "latest")


def discovery_snapshots_dir() -> Path:
    return _cat("discovery", "snapshots")


def discovery_diagnostics_dir() -> Path:
    return _cat("discovery", "diagnostics")


def enriched_latest_dir() -> Path:
    return _cat("enriched", "latest")


def enriched_snapshots_dir() -> Path:
    return _cat("enriched", "snapshots")


def exports_manual_dir() -> Path:
    return _cat("exports", "manual")


def downloads_dir() -> Path:
    return _cat("_downloads")


def cleanup_logs_dir() -> Path:
    return _cat("_cleanup_logs")


# ---------------------------------------------------------------------------
# Ensure directory structure
# ---------------------------------------------------------------------------

_ALL_DIRS = (
    reference_latest_dir,
    reference_snapshots_dir,
    discovery_latest_dir,
    discovery_snapshots_dir,
    discovery_diagnostics_dir,
    enriched_latest_dir,
    enriched_snapshots_dir,
    exports_manual_dir,
    downloads_dir,
    cleanup_logs_dir,
)


def ensure_structure() -> None:
    """Create the full LineItemRates directory tree if it doesn't exist."""
    for factory in _ALL_DIRS:
        factory().mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Run ID
# ---------------------------------------------------------------------------

def make_run_id(tag: str = "AUTO") -> str:
    """Return a deterministic, grep-friendly run ID: YYYYMMDD_HHMMSS_TAG."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_tag = "".join(ch for ch in (tag or "AUTO") if ch.isalnum() or ch == "_")[:12]
    return f"{ts}_{safe_tag}"


# ---------------------------------------------------------------------------
# Path builders for specific output files
# ---------------------------------------------------------------------------

# --- reference ---

def get_reference_paths(run_id: str) -> dict[str, Path]:
    """Return dict with snapshot + latest paths for reference capture."""
    snap = reference_snapshots_dir()
    lat = reference_latest_dir()
    return {
        "snapshot_csv": snap / f"ServiceTypes_reference_{run_id}.csv",
        "snapshot_xlsx": snap / f"ServiceTypes_reference_{run_id}.xlsx",
        "latest_csv": lat / "ServiceTypes_latest.csv",
        "latest_xlsx": lat / "ServiceTypes_latest.xlsx",
    }


# --- discovery ---

def get_discovery_paths(run_id: str) -> dict[str, Path]:
    """Return dict with snapshot + latest + diagnostics paths for discovery."""
    snap = discovery_snapshots_dir()
    lat = discovery_latest_dir()
    diag = discovery_diagnostics_dir() / run_id
    return {
        "snapshot_csv": snap / f"AppointmentItemDiscovery_{run_id}.csv",
        "snapshot_xlsx": snap / f"AppointmentItemDiscovery_{run_id}.xlsx",
        "latest_csv": lat / "AppointmentItemDiscovery_latest.csv",
        "latest_xlsx": lat / "AppointmentItemDiscovery_latest.xlsx",
        "diagnostics_dir": diag,
    }


# --- enriched ---

def get_enriched_paths(run_id: str) -> dict[str, Path]:
    """Return dict with snapshot + latest paths for enriched merge output."""
    snap = enriched_snapshots_dir()
    lat = enriched_latest_dir()
    return {
        "enriched_snapshot_csv": snap / f"ServiceTypes_enriched_{run_id}.csv",
        "enriched_snapshot_xlsx": snap / f"ServiceTypes_enriched_{run_id}.xlsx",
        "enriched_latest_csv": lat / "ServiceTypes_enriched_latest.csv",
        "enriched_latest_xlsx": lat / "ServiceTypes_enriched_latest.xlsx",
        "unmatched_snapshot_csv": snap / f"ServiceTypes_unmatched_discovery_{run_id}.csv",
        "unmatched_snapshot_xlsx": snap / f"ServiceTypes_unmatched_discovery_{run_id}.xlsx",
        "unmatched_latest_csv": lat / "ServiceTypes_unmatched_discovery_latest.csv",
        "unmatched_latest_xlsx": lat / "ServiceTypes_unmatched_discovery_latest.xlsx",
    }


# --- exports ---

def get_export_path(run_id: str, fmt: str = "csv") -> Path:
    """Return path for a manual truth-view export."""
    ext = fmt.lower().strip(".")
    if ext not in ("csv", "xlsx"):
        ext = "csv"
    return exports_manual_dir() / f"TruthView_export_{run_id}.{ext}"


# --- truth root (convenience) ---

def get_truth_root() -> Path:
    """Return the ServiceTypeTruth root directory."""
    return _ROOT / _TRUTH


# --- well-known latest paths (for cross-module imports) ---

def reference_latest_csv() -> Path:
    return reference_latest_dir() / "ServiceTypes_latest.csv"


def reference_latest_xlsx() -> Path:
    return reference_latest_dir() / "ServiceTypes_latest.xlsx"


def discovery_latest_csv() -> Path:
    return discovery_latest_dir() / "AppointmentItemDiscovery_latest.csv"


def discovery_latest_xlsx() -> Path:
    return discovery_latest_dir() / "AppointmentItemDiscovery_latest.xlsx"

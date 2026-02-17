"""
Central storage routing and one-time migration helpers.

All runtime outputs are derived from a single base root:
    TURNPOINT_BASE_ROOT (default: ~/LOCALDB_TurnpointPG)

Compatibility env overrides are still honored:
    PURGED_ARCHIVE_ROOT, PDCC_ROOT, PURGED_WORKER_ROOT, LINE_ITEM_RATES_ROOT
    CLEANED_NEXIS_ROOT, CLIENTS_EXPORT_PATH, TURNPOINT_STATE_DIR
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TRUTH_ROOT_NAME = "ServiceTypeTruth"
MIGRATION_REPORT_FILENAME = "migration_report.json"
DEFAULT_BASE_ROOT = Path.home() / "LOCALDB_TurnpointPG"


def _resolve_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def _env_path(name: str) -> Path | None:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return None
    return _resolve_path(raw)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def base_root() -> Path:
    return _env_path("TURNPOINT_BASE_ROOT") or _resolve_path(DEFAULT_BASE_ROOT)


def purged_clients_root() -> Path:
    return _env_path("PURGED_ARCHIVE_ROOT") or (base_root() / "PurgedClients")


def duplicate_reports_dir() -> Path:
    return purged_clients_root() / "_duplicate_reports"


def pdcc_root() -> Path:
    return _env_path("PDCC_ROOT") or (
        purged_clients_root() / "Package Divided Client Credential (PDCC)"
    )


def pdcc_downloads_dir() -> Path:
    return pdcc_root() / "_downloads"


def latest_purgeable_excel_path() -> Path:
    return pdcc_root() / "latest_purgeable_clients.xlsx"


def package_manifest_path() -> Path:
    return pdcc_root() / "package_manifest.csv"


def purged_worker_root() -> Path:
    return _env_path("PURGED_WORKER_ROOT") or (base_root() / "PurgedWorker")


def worker_downloads_dir() -> Path:
    return purged_worker_root() / "_downloads"


def latest_worker_excel_path() -> Path:
    return purged_worker_root() / "latest_workers.xlsx"


def worker_manifest_path() -> Path:
    return purged_worker_root() / "worker_manifest.csv"


def cleaned_nexis_root() -> Path:
    return _env_path("CLEANED_NEXIS_ROOT") or (purged_worker_root() / "CLEANEDFORNEXIS")


def clients_export_path() -> Path:
    return _env_path("CLIENTS_EXPORT_PATH") or (
        purged_clients_root() / "FormatforClient(Nexis)" / "clients-data.csv"
    )


def line_item_root() -> Path:
    return _env_path("LINE_ITEM_RATES_ROOT") or (base_root() / "LineItemRates")


def truth_root() -> Path:
    return line_item_root() / TRUTH_ROOT_NAME


def truth_config_dir() -> Path:
    return truth_root() / "_config"


def state_dir() -> Path:
    return _env_path("TURNPOINT_STATE_DIR") or (line_item_root() / "_state")


def purger_state_file() -> Path:
    return state_dir() / "purger_state.json"


def worker_state_file() -> Path:
    return state_dir() / "worker_state.json"


def migration_report_path() -> Path:
    return state_dir() / MIGRATION_REPORT_FILENAME


def _service_truth_dirs() -> tuple[Path, ...]:
    root = truth_root()
    return (
        root / "reference" / "latest",
        root / "reference" / "snapshots",
        root / "discovery" / "latest",
        root / "discovery" / "snapshots",
        root / "discovery" / "diagnostics",
        root / "enriched" / "latest",
        root / "enriched" / "snapshots",
        root / "exports" / "manual",
        root / "_downloads",
        root / "_cleanup_logs",
        root / "variants" / "latest",
        root / "variants" / "snapshots",
        root / "variants" / "diagnostics",
        root / "variants" / "checkpoints",
        truth_config_dir(),
    )


def ensure_storage_structure() -> None:
    required_dirs = (
        purged_clients_root(),
        duplicate_reports_dir(),
        pdcc_root(),
        pdcc_downloads_dir(),
        purged_worker_root(),
        worker_downloads_dir(),
        cleaned_nexis_root(),
        clients_export_path().parent,
        line_item_root(),
        state_dir(),
    ) + _service_truth_dirs()

    for path in required_dirs:
        path.mkdir(parents=True, exist_ok=True)


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except Exception:
        return str(a) == str(b)


def _append_entry(
    entries: list[dict[str, Any]],
    *,
    source: Path,
    destination: Path,
    kind: str,
    action: str,
    detail: str = "",
) -> None:
    entries.append(
        {
            "kind": kind,
            "source": str(source),
            "destination": str(destination),
            "action": action,
            "detail": detail,
        }
    )


def _prune_empty_dirs(path: Path) -> None:
    current = path
    while True:
        if not current.exists() or not current.is_dir():
            break
        try:
            next(current.iterdir())
            break
        except StopIteration:
            current.rmdir()
            current = current.parent
        except Exception:
            break


def _merge_directory(source: Path, destination: Path, entries: list[dict[str, Any]]) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for child in sorted(source.iterdir(), key=lambda p: p.name.lower()):
        target = destination / child.name
        if child.is_dir():
            if target.exists() and target.is_file():
                _append_entry(
                    entries,
                    source=child,
                    destination=target,
                    kind="directory",
                    action="conflict_destination_file_kept",
                    detail="Destination path is a file; source directory left in place.",
                )
                continue
            if not target.exists():
                try:
                    shutil.move(str(child), str(target))
                    _append_entry(
                        entries,
                        source=child,
                        destination=target,
                        kind="directory",
                        action="moved",
                    )
                    continue
                except Exception as exc:
                    _append_entry(
                        entries,
                        source=child,
                        destination=target,
                        kind="directory",
                        action="move_failed_fallback_merge",
                        detail=str(exc),
                    )
                    target.mkdir(parents=True, exist_ok=True)
            _merge_directory(child, target, entries)
            _prune_empty_dirs(child)
            continue

        if target.exists():
            _append_entry(
                entries,
                source=child,
                destination=target,
                kind="file",
                action="conflict_destination_file_kept",
                detail="Destination file exists; source file left in place.",
            )
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(child), str(target))
            _append_entry(
                entries,
                source=child,
                destination=target,
                kind="file",
                action="moved",
            )
        except Exception as exc:
            shutil.copy2(child, target)
            child.unlink(missing_ok=True)
            _append_entry(
                entries,
                source=child,
                destination=target,
                kind="file",
                action="copied_after_move_failure",
                detail=str(exc),
            )


def _migrate_directory(source: Path, destination: Path, entries: list[dict[str, Any]]) -> None:
    if not source.exists():
        _append_entry(
            entries,
            source=source,
            destination=destination,
            kind="directory",
            action="missing_source",
        )
        return
    if _same_path(source, destination):
        _append_entry(
            entries,
            source=source,
            destination=destination,
            kind="directory",
            action="same_path_skipped",
        )
        return

    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        try:
            shutil.move(str(source), str(destination))
            _append_entry(
                entries,
                source=source,
                destination=destination,
                kind="directory",
                action="moved",
            )
            return
        except Exception as exc:
            _append_entry(
                entries,
                source=source,
                destination=destination,
                kind="directory",
                action="move_failed_fallback_merge",
                detail=str(exc),
            )

    _merge_directory(source, destination, entries)
    _prune_empty_dirs(source)
    _append_entry(
        entries,
        source=source,
        destination=destination,
        kind="directory",
        action="merged",
    )


def _migrate_file(source: Path, destination: Path, entries: list[dict[str, Any]]) -> None:
    if not source.exists():
        _append_entry(
            entries,
            source=source,
            destination=destination,
            kind="file",
            action="missing_source",
        )
        return
    if _same_path(source, destination):
        _append_entry(
            entries,
            source=source,
            destination=destination,
            kind="file",
            action="same_path_skipped",
        )
        return

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        _append_entry(
            entries,
            source=source,
            destination=destination,
            kind="file",
            action="conflict_destination_file_kept",
            detail="Destination file exists; source file retained.",
        )
        return

    shutil.copy2(source, destination)
    _append_entry(
        entries,
        source=source,
        destination=destination,
        kind="file",
        action="copied",
    )


def _legacy_pairs() -> list[dict[str, Path | str]]:
    home = Path.home()
    repo_root = Path(__file__).resolve().parent
    return [
        {
            "kind": "directory",
            "source": home / "PurgedClients",
            "destination": purged_clients_root(),
        },
        {
            "kind": "directory",
            "source": home / "PurgedClients" / "_duplicate_reports",
            "destination": duplicate_reports_dir(),
        },
        {
            "kind": "directory",
            "source": home / "Purged Client" / "Package Divided Client Credential (PDCC)",
            "destination": pdcc_root(),
        },
        {
            "kind": "directory",
            "source": home / "PurgedWorker",
            "destination": purged_worker_root(),
        },
        {
            "kind": "directory",
            "source": home / "PurgedWorker" / "_downloads",
            "destination": worker_downloads_dir(),
        },
        {
            "kind": "directory",
            "source": home / "LineItemRates",
            "destination": line_item_root(),
        },
        {
            "kind": "file",
            "source": home / ".turnpoint_purger" / "purger_state.json",
            "destination": purger_state_file(),
        },
        {
            "kind": "file",
            "source": home / ".turnpoint_purger" / "worker_state.json",
            "destination": worker_state_file(),
        },
        {
            "kind": "directory",
            "source": home / "CLEANEDFORNEXIS",
            "destination": cleaned_nexis_root(),
        },
        {
            "kind": "file",
            "source": repo_root / "FormatforClient(Nexis)" / "clients-data.csv",
            "destination": clients_export_path(),
        },
    ]


def _summarize(entries: list[dict[str, Any]]) -> dict[str, Any]:
    by_action: dict[str, int] = {}
    for entry in entries:
        action = str(entry.get("action", "unknown"))
        by_action[action] = by_action.get(action, 0) + 1
    return {
        "total_entries": len(entries),
        "actions": by_action,
    }


def auto_migrate_legacy_outputs(*, force: bool = False) -> dict[str, Any]:
    """
    Perform one-time migration from legacy home/repo locations.

    A report marker is written to LineItemRates/_state/migration_report.json.
    Subsequent calls are no-op unless force=True.
    """
    ensure_storage_structure()
    report_path = migration_report_path()

    if report_path.exists() and not force:
        try:
            with report_path.open("r", encoding="utf-8") as fh:
                existing = json.load(fh)
            if isinstance(existing, dict):
                return existing
        except Exception:
            return {
                "status": "already_migrated",
                "report_path": str(report_path),
            }

    entries: list[dict[str, Any]] = []
    for pair in _legacy_pairs():
        source = pair["source"]
        destination = pair["destination"]
        kind = pair["kind"]
        if not isinstance(source, Path) or not isinstance(destination, Path):
            continue
        if kind == "directory":
            _migrate_directory(source, destination, entries)
        else:
            _migrate_file(source, destination, entries)

    report: dict[str, Any] = {
        "status": "completed",
        "version": 1,
        "ran_at_utc": _utc_iso(),
        "base_root": str(base_root()),
        "report_path": str(report_path),
        "summary": _summarize(entries),
        "entries": entries,
    }

    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    return report

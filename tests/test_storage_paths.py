import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import line_item_paths  # noqa: E402
import purger_state  # noqa: E402
import storage_paths  # noqa: E402
import worker_state  # noqa: E402


def _clear_path_envs(monkeypatch):
    for key in (
        "PURGED_ARCHIVE_ROOT",
        "PDCC_ROOT",
        "PURGED_WORKER_ROOT",
        "LINE_ITEM_RATES_ROOT",
        "CLEANED_NEXIS_ROOT",
        "CLIENTS_EXPORT_PATH",
        "TURNPOINT_STATE_DIR",
    ):
        monkeypatch.delenv(key, raising=False)


def test_storage_defaults_derive_from_base_root(monkeypatch, tmp_path):
    base = (tmp_path / "LOCALDB_TurnpointPG").resolve()
    monkeypatch.setenv("TURNPOINT_BASE_ROOT", str(base))
    _clear_path_envs(monkeypatch)

    assert storage_paths.purged_clients_root() == base / "PurgedClients"
    assert storage_paths.pdcc_root() == base / "PurgedClients" / "Package Divided Client Credential (PDCC)"
    assert storage_paths.purged_worker_root() == base / "PurgedWorker"
    assert storage_paths.cleaned_nexis_root() == base / "PurgedWorker" / "CLEANEDFORNEXIS"
    assert storage_paths.line_item_root() == base / "LineItemRates"
    assert storage_paths.state_dir() == base / "LineItemRates" / "_state"
    assert storage_paths.clients_export_path() == (
        base / "PurgedClients" / "FormatforClient(Nexis)" / "clients-data.csv"
    )


def test_storage_env_overrides_take_precedence(monkeypatch, tmp_path):
    base = (tmp_path / "LOCALDB_TurnpointPG").resolve()
    clients_override = (tmp_path / "clients_override").resolve()
    workers_override = (tmp_path / "workers_override").resolve()
    pdcc_override = (tmp_path / "pdcc_override").resolve()
    line_item_override = (tmp_path / "line_item_override").resolve()
    cleaned_override = (tmp_path / "cleaned_override").resolve()
    clients_export_override = (tmp_path / "clients.csv").resolve()
    state_override = (tmp_path / "state_override").resolve()

    monkeypatch.setenv("TURNPOINT_BASE_ROOT", str(base))
    monkeypatch.setenv("PURGED_ARCHIVE_ROOT", str(clients_override))
    monkeypatch.setenv("PURGED_WORKER_ROOT", str(workers_override))
    monkeypatch.setenv("PDCC_ROOT", str(pdcc_override))
    monkeypatch.setenv("LINE_ITEM_RATES_ROOT", str(line_item_override))
    monkeypatch.setenv("CLEANED_NEXIS_ROOT", str(cleaned_override))
    monkeypatch.setenv("CLIENTS_EXPORT_PATH", str(clients_export_override))
    monkeypatch.setenv("TURNPOINT_STATE_DIR", str(state_override))

    assert storage_paths.purged_clients_root() == clients_override
    assert storage_paths.purged_worker_root() == workers_override
    assert storage_paths.pdcc_root() == pdcc_override
    assert storage_paths.line_item_root() == line_item_override
    assert storage_paths.cleaned_nexis_root() == cleaned_override
    assert storage_paths.clients_export_path() == clients_export_override
    assert storage_paths.state_dir() == state_override


def test_line_item_paths_uses_central_root(monkeypatch, tmp_path):
    base = (tmp_path / "LOCALDB_TurnpointPG").resolve()
    _clear_path_envs(monkeypatch)
    monkeypatch.setenv("TURNPOINT_BASE_ROOT", str(base))
    assert line_item_paths.get_root() == base / "LineItemRates"
    assert line_item_paths.get_truth_root() == base / "LineItemRates" / "ServiceTypeTruth"


def test_state_files_write_under_rerouted_state_dir(monkeypatch, tmp_path):
    base = (tmp_path / "LOCALDB_TurnpointPG").resolve()
    _clear_path_envs(monkeypatch)
    monkeypatch.setenv("TURNPOINT_BASE_ROOT", str(base))

    purger_state.reset_state()
    worker_state.reset_worker_state()

    purger_state.record_purge_event(
        universal_id=100001,
        turnpoint_id="12345",
        client_name="Client One",
        success=True,
        bytes_written=12,
        timestamp_iso="2026-02-17T00:00:00Z",
        operator="Tester",
    )
    worker_state.record_worker_event(
        universal_id=200001,
        worker_id="54321",
        worker_name="Worker One",
        success=True,
        bytes_written=34,
        timestamp_iso="2026-02-17T00:00:00Z",
        operator="Tester",
    )

    assert (base / "LineItemRates" / "_state" / "purger_state.json").exists()
    assert (base / "LineItemRates" / "_state" / "worker_state.json").exists()


def test_migration_moves_directory_when_destination_missing(monkeypatch, tmp_path):
    home_root = (tmp_path / "legacy_home").resolve()
    base = (tmp_path / "LOCALDB_TurnpointPG").resolve()
    _clear_path_envs(monkeypatch)
    monkeypatch.setenv("HOME", str(home_root))
    monkeypatch.setenv("TURNPOINT_BASE_ROOT", str(base))

    legacy_clients = home_root / "PurgedClients"
    legacy_clients.mkdir(parents=True, exist_ok=True)
    (legacy_clients / "legacy.csv").write_text("x", encoding="utf-8")

    storage_paths.auto_migrate_legacy_outputs(force=True)

    assert (base / "PurgedClients" / "legacy.csv").exists()
    assert not legacy_clients.exists()


def test_migration_merges_when_destination_exists(monkeypatch, tmp_path):
    home_root = (tmp_path / "legacy_home").resolve()
    base = (tmp_path / "LOCALDB_TurnpointPG").resolve()
    _clear_path_envs(monkeypatch)
    monkeypatch.setenv("HOME", str(home_root))
    monkeypatch.setenv("TURNPOINT_BASE_ROOT", str(base))

    legacy_worker = home_root / "PurgedWorker"
    legacy_worker.mkdir(parents=True, exist_ok=True)
    (legacy_worker / "legacy_worker.csv").write_text("a", encoding="utf-8")

    destination_worker = base / "PurgedWorker"
    destination_worker.mkdir(parents=True, exist_ok=True)
    (destination_worker / "existing_worker.csv").write_text("b", encoding="utf-8")

    storage_paths.auto_migrate_legacy_outputs(force=True)

    assert (destination_worker / "legacy_worker.csv").exists()
    assert (destination_worker / "existing_worker.csv").exists()


def test_migration_is_idempotent_without_force(monkeypatch, tmp_path):
    home_root = (tmp_path / "legacy_home").resolve()
    base = (tmp_path / "LOCALDB_TurnpointPG").resolve()
    _clear_path_envs(monkeypatch)
    monkeypatch.setenv("HOME", str(home_root))
    monkeypatch.setenv("TURNPOINT_BASE_ROOT", str(base))

    legacy_line_items = home_root / "LineItemRates"
    legacy_line_items.mkdir(parents=True, exist_ok=True)
    (legacy_line_items / "old.txt").write_text("old", encoding="utf-8")

    first = storage_paths.auto_migrate_legacy_outputs(force=True)
    second = storage_paths.auto_migrate_legacy_outputs(force=False)

    assert first["report_path"] == second["report_path"]
    assert first["ran_at_utc"] == second["ran_at_utc"]
    assert (base / "LineItemRates" / "old.txt").exists()


def test_migration_logs_file_conflicts(monkeypatch, tmp_path):
    home_root = (tmp_path / "legacy_home").resolve()
    base = (tmp_path / "LOCALDB_TurnpointPG").resolve()
    _clear_path_envs(monkeypatch)
    monkeypatch.setenv("HOME", str(home_root))
    monkeypatch.setenv("TURNPOINT_BASE_ROOT", str(base))

    legacy_state = home_root / ".turnpoint_purger" / "purger_state.json"
    legacy_state.parent.mkdir(parents=True, exist_ok=True)
    legacy_state.write_text(json.dumps({"legacy": True}), encoding="utf-8")

    destination_state = base / "LineItemRates" / "_state" / "purger_state.json"
    destination_state.parent.mkdir(parents=True, exist_ok=True)
    destination_state.write_text(json.dumps({"new": True}), encoding="utf-8")

    report = storage_paths.auto_migrate_legacy_outputs(force=True)

    matches = [
        e
        for e in report.get("entries", [])
        if e.get("source") == str(legacy_state)
        and e.get("action") == "conflict_destination_file_kept"
    ]
    assert matches, "Expected conflict entry for legacy purger_state.json"

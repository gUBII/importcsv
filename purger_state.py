import json
import threading
from pathlib import Path

DEFAULT_START_ID = 100001
STATE_DIR = Path.home() / ".turnpoint_purger"
STATE_FILE = STATE_DIR / "purger_state.json"
HISTORY_LIMIT = 200

_state_lock = threading.Lock()


def _default_state():
    return {
        "next_universal_id": DEFAULT_START_ID,
        "purged_count": 0,
        "clients": {},
        "history": [],
    }


def _coerce_state(raw):
    base = _default_state()
    if not isinstance(raw, dict):
        return base
    try:
        base["next_universal_id"] = int(raw.get("next_universal_id", DEFAULT_START_ID))
    except (TypeError, ValueError):
        base["next_universal_id"] = DEFAULT_START_ID
    try:
        base["purged_count"] = int(raw.get("purged_count", 0))
    except (TypeError, ValueError):
        base["purged_count"] = 0
    base["clients"] = raw.get("clients", {})
    base["history"] = raw.get("history", [])
    return base


def _read_state():
    if not STATE_FILE.exists():
        return _default_state()
    try:
        with STATE_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return _default_state()
    return _coerce_state(data)


def _write_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = STATE_FILE.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    tmp_path.replace(STATE_FILE)


def get_purge_statistics():
    """Return a snapshot of purge counters (thread-safe)."""
    with _state_lock:
        return _read_state().copy()


def reserve_universal_sequence():
    """Return the current universal ID slot and cumulative purge count."""
    with _state_lock:
        state = _read_state()
        return state["next_universal_id"], state["purged_count"]


def get_client_last_purge(client_id):
    """Return metadata for the last purge of the provided TurnPoint client ID."""
    client_id = str(client_id)
    with _state_lock:
        state = _read_state()
        return state.get("clients", {}).get(client_id)


def get_recent_history(limit=10):
    """Return the most recent purge events (success + failure)."""
    with _state_lock:
        history = _read_state().get("history", [])
    return history[-limit:]


def record_purge_event(
    *,
    universal_id,
    turnpoint_id,
    client_name,
    success,
    bytes_written,
    timestamp_iso,
    operator=None,
):
    """
    Persist the outcome of a purge attempt and update counters as needed.
    Returns the updated state snapshot.
    """
    used_value = int(universal_id)
    turnpoint_id = str(turnpoint_id)
    entry = {
        "universal_id": used_value,
        "turnpoint_id": turnpoint_id,
        "client_name": client_name or "",
        "success": bool(success),
        "bytes": int(bytes_written),
        "timestamp": timestamp_iso,
        "operator": operator or "",
    }

    with _state_lock:
        state = _read_state()
        history = state.setdefault("history", [])
        history.append(entry)
        if len(history) > HISTORY_LIMIT:
            del history[:-HISTORY_LIMIT]

        if success:
            clients = state.setdefault("clients", {})
            clients[turnpoint_id] = {
                "universal_id": used_value,
                "client_name": client_name or "",
                "bytes": int(bytes_written),
                "timestamp": timestamp_iso,
                "operator": operator or "",
            }
            state["purged_count"] = int(state.get("purged_count", 0)) + 1

        state["next_universal_id"] = max(state.get("next_universal_id", DEFAULT_START_ID), used_value + 1)

        _write_state(state)
        return state.copy()


def reset_state():
    """Delete the persisted purge state file (used by reset command)."""
    with _state_lock:
        if STATE_FILE.exists():
            STATE_FILE.unlink()

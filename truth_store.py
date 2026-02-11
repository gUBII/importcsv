"""
In-memory Truth Store for Service Type rate / item-number resolution.

Keyed by Service Type ID. Tracks candidate values by source,
resolves truth fields via precedence, detects per-field conflicts,
and computes row-level status (red / yellow / blue).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TruthCandidate:
    value: str
    source: str          # "reference" | "discovery" | "details"
    updated_utc: str = ""


@dataclass
class TruthRecord:
    service_type_id: str
    parent_service_type: str = ""
    service_variant_label: str = ""

    # Candidates per field per source
    rate_candidates: Dict[str, TruthCandidate] = field(default_factory=dict)
    item_candidates: Dict[str, TruthCandidate] = field(default_factory=dict)

    # Resolved truth
    truth_rate: str = ""
    truth_rate_source: str = ""
    truth_item_number: str = ""
    truth_item_source: str = ""

    # Status: "red" | "yellow" | "blue"
    status: str = "red"
    rate_conflict: bool = False
    item_conflict: bool = False

    updated_utc: str = ""

    # Extra reference fields (carried for inspector / export)
    service_type_link: str = ""
    def_rate: str = ""
    service_code: str = ""


# ---------------------------------------------------------------------------
# Precedence order (highest first)
# ---------------------------------------------------------------------------

_SOURCE_PRECEDENCE = ("details", "discovery", "reference")


def _pick_truth(candidates: Dict[str, TruthCandidate]) -> tuple[str, str]:
    """Return (value, source) for the highest-precedence non-empty candidate."""
    for src in _SOURCE_PRECEDENCE:
        cand = candidates.get(src)
        if cand and cand.value:
            return cand.value, src
    return "", ""


def _detect_conflict(candidates: Dict[str, TruthCandidate]) -> bool:
    """Return True if 2+ non-empty candidates disagree."""
    values = set()
    for cand in candidates.values():
        v = _normalize(cand.value)
        if v:
            values.add(v)
    return len(values) >= 2


def _normalize(value: str) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _compute_status(truth_rate: str, truth_item: str) -> str:
    has_rate = bool(truth_rate.strip())
    has_item = bool(truth_item.strip())
    if has_rate and has_item:
        return "blue"
    if has_rate or has_item:
        return "yellow"
    return "red"


# ---------------------------------------------------------------------------
# TruthStore
# ---------------------------------------------------------------------------

class TruthStore:
    """Thread-unsafe in-memory truth store. UI must call from main thread."""

    def __init__(self) -> None:
        self._records: Dict[str, TruthRecord] = {}
        self.on_change: Optional[Callable[[], None]] = None

    # -- public queries --

    def get_all_records(self) -> List[TruthRecord]:
        return list(self._records.values())

    def get_parent_groups(self) -> List[str]:
        groups: set[str] = set()
        for rec in self._records.values():
            if rec.parent_service_type:
                groups.add(rec.parent_service_type)
        return sorted(groups)

    def get_records_for_parent(self, parent: str) -> List[TruthRecord]:
        if not parent:
            return self.get_all_records()
        return [r for r in self._records.values() if r.parent_service_type == parent]

    def get_record(self, service_type_id: str) -> Optional[TruthRecord]:
        return self._records.get(service_type_id)

    def get_status_counts(self) -> Dict[str, int]:
        counts = {"red": 0, "yellow": 0, "blue": 0, "conflicts": 0}
        for rec in self._records.values():
            counts[rec.status] = counts.get(rec.status, 0) + 1
            if rec.rate_conflict or rec.item_conflict:
                counts["conflicts"] += 1
        return counts

    def record_count(self) -> int:
        return len(self._records)

    def clear(self) -> None:
        self._records.clear()
        self._fire_change()

    # -- upsert methods --

    def upsert_reference(self, row: dict) -> None:
        """Ingest a reference row from Capture Live Rates / ImportCSV."""
        sid = _normalize(str(row.get("ID", "") or ""))
        if not sid:
            return

        rec = self._ensure(sid)
        now = _utc_now()

        # Reference uses "Service Type" as the label
        stype = _normalize(str(row.get("Service Type", "") or ""))
        if stype:
            if not rec.service_variant_label:
                rec.service_variant_label = stype
            if not rec.parent_service_type:
                rec.parent_service_type = stype

        # Def. Rate → rate candidate
        def_rate = _normalize(str(row.get("Def. Rate", "") or ""))
        if def_rate:
            rec.rate_candidates["reference"] = TruthCandidate(
                value=def_rate, source="reference", updated_utc=now,
            )
            rec.def_rate = def_rate

        # Service Code → item candidate
        scode = _normalize(str(row.get("Service Code", "") or ""))
        if scode:
            rec.item_candidates["reference"] = TruthCandidate(
                value=scode, source="reference", updated_utc=now,
            )
            rec.service_code = scode

        # ServiceTypeLink
        link = _normalize(str(row.get("ServiceTypeLink", "") or ""))
        if link:
            rec.service_type_link = link

        self._resolve(rec)
        self._fire_change()

    def upsert_discovery(self, row: dict) -> None:
        """Ingest a discovery row from Appointment Item Discovery."""
        sid = _normalize(str(row.get("Service Type ID", "") or ""))
        if not sid:
            return

        rec = self._ensure(sid)
        now = _utc_now()

        # Labels
        parent = _normalize(str(row.get("Parent Service Type", "") or ""))
        variant = _normalize(str(row.get("Service Variant Label", "") or ""))
        if parent:
            rec.parent_service_type = parent
        if variant:
            rec.service_variant_label = variant

        # Rate candidate
        rate = _normalize(str(row.get("Rate", "") or ""))
        if rate:
            rec.rate_candidates["discovery"] = TruthCandidate(
                value=rate, source="discovery", updated_utc=now,
            )

        # Item Number candidate
        item = _normalize(str(row.get("Item Number", "") or ""))
        if item:
            rec.item_candidates["discovery"] = TruthCandidate(
                value=item, source="discovery", updated_utc=now,
            )

        self._resolve(rec)
        self._fire_change()

    def upsert_details(
        self, service_type_id: str, item_number: str = "", rate: str = "",
    ) -> None:
        """Ingest enrichment from a service-type-details page."""
        sid = _normalize(service_type_id)
        if not sid:
            return

        rec = self._ensure(sid)
        now = _utc_now()

        if _normalize(item_number):
            rec.item_candidates["details"] = TruthCandidate(
                value=_normalize(item_number), source="details", updated_utc=now,
            )
        if _normalize(rate):
            rec.rate_candidates["details"] = TruthCandidate(
                value=_normalize(rate), source="details", updated_utc=now,
            )

        self._resolve(rec)
        self._fire_change()

    # -- internals --

    def _ensure(self, service_type_id: str) -> TruthRecord:
        rec = self._records.get(service_type_id)
        if rec is None:
            rec = TruthRecord(service_type_id=service_type_id)
            self._records[service_type_id] = rec
        return rec

    def _resolve(self, rec: TruthRecord) -> None:
        rec.truth_rate, rec.truth_rate_source = _pick_truth(rec.rate_candidates)
        rec.truth_item_number, rec.truth_item_source = _pick_truth(rec.item_candidates)
        rec.rate_conflict = _detect_conflict(rec.rate_candidates)
        rec.item_conflict = _detect_conflict(rec.item_candidates)
        rec.status = _compute_status(rec.truth_rate, rec.truth_item_number)
        rec.updated_utc = _utc_now()

    def _fire_change(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

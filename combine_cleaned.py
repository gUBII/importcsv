#!/usr/bin/env python3
"""
Combine all CLEANEDFORNEXIS worker CSVs into a single CSV and JSON file.

Usage:
    python combine_cleaned.py [--root /path/to/CLEANEDFORNEXIS] [--out combined.csv]

Defaults:
    root: ~/LOCALDB_TurnpointPG/PurgedWorker/CLEANEDFORNEXIS
    out:  combined_workers.csv (alongside the root)
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import List, Dict

from nexis_uploader import discover_workers, build_nexis_employee
import storage_paths


def discover_csvs(root: Path) -> List[Path]:
    return sorted(p for p in root.glob("*.csv") if p.is_file())


def read_rows(csv_path: Path) -> List[Dict[str, str]]:
    with csv_path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return list(reader)


def combine(root: Path, out_csv: Path, out_json: Path) -> None:
    files = discover_csvs(root)
    if not files:
        raise SystemExit(f"No CSV files found in {root}")

    all_rows: List[Dict[str, str]] = []
    headers: List[str] = []
    for path in files:
        rows = read_rows(path)
        for row in rows:
            for key in row.keys():
                if key not in headers:
                    headers.append(key)
            row["_source_file"] = path.name
            all_rows.append(row)

    # Write combined CSV of raw worker detail
    with out_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=headers + ["_source_file"])
        writer.writeheader()
        for row in all_rows:
            writer.writerow({h: row.get(h, "") for h in writer.fieldnames})

    # Write combined JSON of raw worker detail
    with out_json.open("w", encoding="utf-8") as fh:
        json.dump(all_rows, fh, indent=2, ensure_ascii=False)

    # Nexis formatted export
    workers = discover_workers(root)
    nexis_records = [build_nexis_employee(w) for w in workers]
    nexis_headers: List[str] = []
    for rec in nexis_records:
        for key in rec.keys():
            if key not in nexis_headers:
                nexis_headers.append(key)
    nexis_csv = out_csv.with_name(out_csv.stem + "_nexis.csv")
    nexis_json = out_csv.with_name(out_csv.stem + "_nexis.json")
    with nexis_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=nexis_headers)
        writer.writeheader()
        for rec in nexis_records:
            writer.writerow({h: rec.get(h, "") for h in nexis_headers})
    with nexis_json.open("w", encoding="utf-8") as fh:
        json.dump(nexis_records, fh, indent=2, ensure_ascii=False)

    print(f"Combined {len(all_rows)} rows from {len(files)} files")
    print(f"CSV -> {out_csv}")
    print(f"JSON -> {out_json}")
    print(f"Nexis CSV -> {nexis_csv}")
    print(f"Nexis JSON -> {nexis_json}")


def main() -> None:
    storage_paths.ensure_storage_structure()
    try:
        storage_paths.auto_migrate_legacy_outputs()
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="Combine CLEANEDFORNEXIS CSVs into one CSV and JSON.")
    parser.add_argument(
        "--root",
        default=str(storage_paths.cleaned_nexis_root()),
        help="Path to CLEANEDFORNEXIS folder.",
    )
    parser.add_argument("--out", default="combined_workers.csv", help="Output CSV filename (JSON will match stem).")
    args = parser.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not root.exists():
        raise SystemExit(f"Root path not found: {root}")

    out_csv = (root / args.out).resolve()
    out_json = out_csv.with_suffix(".json")

    combine(root, out_csv, out_json)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Export directory tree snapshots to docs/directories.

Usage:
    python scripts/export_directory_trees.py
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = REPO_ROOT / "docs" / "directories"
IMPORTCSV_DOC_ROOT = DOCS_ROOT / "importcsv"
MOOFASA_DOC_ROOT = DOCS_ROOT / "moofasa"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _base_root() -> Path:
    return Path(
        os.getenv("TURNPOINT_BASE_ROOT", str(Path.home() / "LOCALDB_TurnpointPG"))
    ).expanduser().resolve()


def _iter_tree_lines(root: Path, *, max_depth: int, max_entries_per_dir: int) -> list[str]:
    lines: list[str] = []
    root = root.resolve()

    def walk(path: Path, depth: int) -> None:
        indent = "  " * depth
        if depth == 0:
            lines.append(f"- {path.name}/")
        else:
            lines.append(f"{indent}- {path.name}/")

        if depth >= max_depth:
            lines.append(f"{indent}  - ... (max depth reached)")
            return

        try:
            children = sorted(
                list(path.iterdir()),
                key=lambda p: (not p.is_dir(), p.name.lower()),
            )
        except PermissionError:
            lines.append(f"{indent}  - [permission denied]")
            return
        except FileNotFoundError:
            lines.append(f"{indent}  - [not found]")
            return

        shown = children[:max_entries_per_dir]
        for child in shown:
            if child.is_dir():
                walk(child, depth + 1)
            else:
                lines.append(f"{indent}  - {child.name}")

        remainder = len(children) - len(shown)
        if remainder > 0:
            lines.append(f"{indent}  - ... ({remainder} more entries)")

    walk(root, 0)
    return lines


def _render_snapshot(root: Path, *, max_depth: int, max_entries_per_dir: int) -> str:
    header = [
        "# Directory Tree Snapshot",
        f"source: {root}",
        f"generated_utc: {_utc_now()}",
        f"max_depth: {max_depth}",
        f"max_entries_per_dir: {max_entries_per_dir}",
        "",
    ]
    if not root.exists():
        return "\n".join(header + ["[missing]"])
    if not root.is_dir():
        return "\n".join(header + ["[not a directory]"])
    lines = _iter_tree_lines(
        root,
        max_depth=max_depth,
        max_entries_per_dir=max_entries_per_dir,
    )
    return "\n".join(header + lines) + "\n"


def _write_snapshot(output_path: Path, source_path: Path, *, max_depth: int, max_entries_per_dir: int) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        _render_snapshot(
            source_path,
            max_depth=max_depth,
            max_entries_per_dir=max_entries_per_dir,
        ),
        encoding="utf-8",
    )
    print(f"wrote: {output_path}")


def _write_index(max_depth: int, max_entries_per_dir: int) -> None:
    index_path = DOCS_ROOT / "README.md"
    lines = [
        "# Directory Tree Docs",
        "",
        "These files are generated snapshots of key directory trees.",
        "",
        "Regenerate:",
        "```bash",
        "python scripts/export_directory_trees.py",
        "```",
        "",
        f"Default limits: max_depth={max_depth}, max_entries_per_dir={max_entries_per_dir}.",
        "",
        "Output folders:",
        "- `docs/directories/importcsv`",
        "- `docs/directories/moofasa`",
        "",
    ]
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote: {index_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export directory tree snapshots.")
    parser.add_argument(
        "--max-depth",
        type=int,
        default=3,
        help="Maximum directory depth to expand (default: 3).",
    )
    parser.add_argument(
        "--max-entries-per-dir",
        type=int,
        default=200,
        help="Max child entries per directory before truncation (default: 200).",
    )
    args = parser.parse_args()

    max_depth = max(1, args.max_depth)
    max_entries = max(10, args.max_entries_per_dir)

    _write_index(max_depth, max_entries)
    _write_snapshot(
        IMPORTCSV_DOC_ROOT / "importcsv.tree.txt",
        REPO_ROOT,
        max_depth=max_depth,
        max_entries_per_dir=max_entries,
    )

    base_root = _base_root()
    targets = [
        ("LOCALDB_TurnpointPG", base_root),
        ("PurgedClients", base_root / "PurgedClients"),
        ("PurgedWorker", base_root / "PurgedWorker"),
        ("PurgedWorker_CLEANEDFORNEXIS", base_root / "PurgedWorker" / "CLEANEDFORNEXIS"),
        ("LineItemRates", base_root / "LineItemRates"),
    ]
    for snapshot_name, source in targets:
        output = MOOFASA_DOC_ROOT / f"{snapshot_name}.tree.txt"
        _write_snapshot(
            output,
            source,
            max_depth=max_depth,
            max_entries_per_dir=max_entries,
        )


if __name__ == "__main__":
    main()

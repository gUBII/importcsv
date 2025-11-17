#!/usr/bin/env python3
"""
Declutter – helper script to remove build artefacts, bytecode caches, and platform
metadata so the repository returns to a clean source-only state.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CLUTTER_DIRS = [
    "build",
    "dist",
    "turnpoint_purger.egg-info",
]
EXTRA_DIR_PATTERNS = [
    "__pycache__",
]
FILE_PATTERNS = [
    ".DS_Store",
]


def remove_path(path: Path) -> bool:
    """Delete the provided path if it exists. Returns True when removed."""
    if not path.exists():
        return False
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            return False
    return True


def declutter(root: Path) -> list[str]:
    """
    Delete standard build artefacts, dist folders, and common temp files.
    Returns a log describing what was removed.
    """
    log: list[str] = []
    for relative in CLUTTER_DIRS:
        target = root / relative
        if remove_path(target):
            log.append(f"Removed directory: {target}")

    for pattern in EXTRA_DIR_PATTERNS:
        for candidate in root.rglob(pattern):
            if candidate.is_dir() and remove_path(candidate):
                log.append(f"Removed directory: {candidate}")

    for pattern in FILE_PATTERNS:
        for candidate in root.rglob(pattern):
            if candidate.is_file() and remove_path(candidate):
                log.append(f"Removed file: {candidate}")

    return log


def main():
    parser = argparse.ArgumentParser(description="Delete build artefacts and clutter.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List the paths that would be removed without deleting them.",
    )
    args = parser.parse_args()

    if args.dry_run:
        planned = []
        for relative in CLUTTER_DIRS:
            target = ROOT / relative
            if target.exists():
                planned.append(target)
        for pattern in EXTRA_DIR_PATTERNS + FILE_PATTERNS:
            for candidate in ROOT.rglob(pattern):
                planned.append(candidate)
        if not planned:
            print("Declutter dry-run: nothing to remove.")
        else:
            print("Declutter dry-run: would remove")
            for path in sorted(planned):
                print(f"  - {path}")
        return

    log = declutter(ROOT)
    if not log:
        print("Declutter complete: nothing to remove.")
    else:
        print("Declutter complete:")
        for entry in log:
            print(f"  - {entry}")


if __name__ == "__main__":
    main()

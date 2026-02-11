#!/usr/bin/env python3
"""
Declutter – helper script to remove build artefacts, bytecode caches, and platform
metadata so the repository returns to a clean source-only state.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent
CLUTTER_DIRS = [
    "build",
    "dist",
    "htmlcov",
    "turnpoint_purger.egg-info",
]
CACHE_DIR_PATTERNS = [
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".hypothesis",
    ".nox",
    ".tox",
    ".ipynb_checkpoints",
]
FILE_PATTERNS = [
    ".DS_Store",
    "Thumbs.db",
    ".coverage",
    ".coverage.*",
    "*.pyc",
    "*.pyo",
]
TEMP_FILE_PATTERNS = [
    "*.tmp",
    "*.temp",
    "*.bak",
    "*.orig",
    "*.rej",
    "*.swp",
    "*.swo",
    "*~",
]
EXCLUDED_DIR_NAMES = {".git"}


def _contains_excluded_part(path: Path) -> bool:
    return any(part in EXCLUDED_DIR_NAMES for part in path.parts)


def _iter_candidates(root: Path, patterns: Iterable[str]):
    for pattern in patterns:
        for candidate in root.rglob(pattern):
            if _contains_excluded_part(candidate):
                continue
            yield candidate


def _collect_paths(root: Path, include_temp_files: bool = True) -> list[Path]:
    targets: list[Path] = []
    for relative in CLUTTER_DIRS:
        target = root / relative
        if target.exists() and not _contains_excluded_part(target):
            targets.append(target)

    for candidate in _iter_candidates(root, CACHE_DIR_PATTERNS):
        if candidate.is_dir():
            targets.append(candidate)

    file_patterns = list(FILE_PATTERNS)
    if include_temp_files:
        file_patterns.extend(TEMP_FILE_PATTERNS)
    for candidate in _iter_candidates(root, file_patterns):
        if candidate.is_file() or candidate.is_symlink():
            targets.append(candidate)

    # De-dupe and remove nested paths when parent is already selected.
    unique_targets = sorted(set(targets), key=lambda p: str(p))
    filtered: list[Path] = []
    for path in unique_targets:
        if any(parent in path.parents for parent in filtered):
            continue
        filtered.append(path)
    return filtered


def remove_path(path: Path) -> bool:
    """Delete the provided path if it exists. Returns True when removed."""
    if not path.exists():
        return False
    if path.is_file() or path.is_symlink():
        try:
            path.unlink()
        except FileNotFoundError:
            return False
    elif path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        return False
    return True


def declutter(root: Path, include_temp_files: bool = True) -> list[str]:
    """
    Delete standard build artefacts, dist folders, and common temp files.
    Returns a log describing what was removed.
    """
    log: list[str] = []
    for target in _collect_paths(root, include_temp_files=include_temp_files):
        is_directory = target.is_dir() and not target.is_symlink()
        if remove_path(target):
            label = "directory" if is_directory else "file"
            log.append(f"Removed {label}: {target}")

    return log


def main():
    parser = argparse.ArgumentParser(description="Delete build artefacts and clutter.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List the paths that would be removed without deleting them.",
    )
    parser.add_argument(
        "--skip-temp-files",
        action="store_true",
        help="Keep generic temp files (*.tmp, *.bak, swap files) and clean caches/build outputs only.",
    )
    args = parser.parse_args()
    include_temp_files = not args.skip_temp_files

    if args.dry_run:
        planned = _collect_paths(ROOT, include_temp_files=include_temp_files)
        if not planned:
            print("Declutter dry-run: nothing to remove.")
        else:
            print("Declutter dry-run: would remove")
            for path in planned:
                print(f"  - {path}")
        return

    log = declutter(ROOT, include_temp_files=include_temp_files)
    if not log:
        print("Declutter complete: nothing to remove.")
    else:
        print(f"Declutter complete: removed {len(log)} path(s).")
        for entry in log:
            print(f"  - {entry}")


if __name__ == "__main__":
    main()

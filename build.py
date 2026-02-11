#!/usr/bin/env python3
"""
Helper script to automate PyInstaller builds for TurnpointPurger.

Examples:
    python build.py --gui        # Build GUI app only
    python build.py --cli        # Build CLI binary only
    python build.py --gui --cli  # Build both
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SPEC_GUI = ROOT / "turnpoint_gui.spec"
SPEC_CLI = ROOT / "turnpoint_cli.spec"
REQUIREMENTS_BUILD = ROOT / "requirements-build.txt"


def ensure_pyinstaller():
    try:
        import PyInstaller  # noqa: F401

        return
    except Exception:
        print("PyInstaller missing – installing build requirements...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS_BUILD)]
        )


def _sync_root_aliases(dist_dir: Path):
    """
    Keep convenience launch artifacts in `dist/` in sync with the platform folder.
    """
    root_dist = ROOT / "dist"
    root_dist.mkdir(parents=True, exist_ok=True)
    if dist_dir.resolve() == root_dist.resolve():
        return

    for binary_name in ("TurnpointPurger", "TurnpointPurgerCLI"):
        source = dist_dir / binary_name
        if source.exists():
            shutil.copy2(source, root_dist / binary_name)

    app_source = dist_dir / "TurnpointPurger.app"
    app_target = root_dist / "TurnpointPurger.app"
    if app_source.exists():
        if app_target.exists():
            shutil.rmtree(app_target)
        shutil.copytree(app_source, app_target)


def run_spec(spec_path: Path) -> Path:
    if not spec_path.exists():
        raise SystemExit(f"Spec file not found: {spec_path}")
    import PyInstaller.__main__

    dist_dir = ROOT / "dist"
    if sys.platform.startswith("darwin"):
        dist_dir = dist_dir / "macos"
    elif sys.platform.startswith("win"):
        dist_dir = dist_dir / "windows"
    else:
        dist_dir = dist_dir / "linux"
    dist_dir.mkdir(parents=True, exist_ok=True)

    PyInstaller.__main__.run(
        [
            "--noconfirm",
            "--distpath",
            str(dist_dir),
            "--workpath",
            str(ROOT / "build"),
            str(spec_path),
        ]
    )
    _sync_root_aliases(dist_dir)
    return dist_dir


def main():
    parser = argparse.ArgumentParser(description="Build PyInstaller bundles.")
    parser.add_argument("--gui", action="store_true", help="Build the GUI app bundle.")
    parser.add_argument("--cli", action="store_true", help="Build the CLI console bundle.")
    args = parser.parse_args()

    if not args.gui and not args.cli:
        parser.error("Select at least one target via --gui and/or --cli.")

    ensure_pyinstaller()

    if args.gui:
        print("Building GUI bundle (turnpoint_gui.spec)...")
        dist_dir = run_spec(SPEC_GUI)
        gui_path = dist_dir / "TurnpointPurger"
        app_path = dist_dir / "TurnpointPurger.app"
        print(f"GUI build complete -> {gui_path}")
        if app_path.exists():
            print(f"GUI app bundle -> {app_path}")

    if args.cli:
        print("Building CLI bundle (turnpoint_cli.spec)...")
        dist_dir = run_spec(SPEC_CLI)
        cli_path = dist_dir / "TurnpointPurgerCLI"
        print(f"CLI build complete -> {cli_path}")


if __name__ == "__main__":
    main()

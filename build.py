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


def run_spec(spec_path: Path):
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
            "--distpath",
            str(dist_dir),
            "--workpath",
            str(ROOT / "build"),
            str(spec_path),
        ]
    )


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
        run_spec(SPEC_GUI)
        print("GUI build complete -> dist/TurnpointPurger")

    if args.cli:
        print("Building CLI bundle (turnpoint_cli.spec)...")
        run_spec(SPEC_CLI)
        print("CLI build complete -> dist/TurnpointPurgerCLI")


if __name__ == "__main__":
    main()

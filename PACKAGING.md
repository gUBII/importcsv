# TurnpointPurger Packaging & Build Guide

## Project layout

- `importcsv.py` – CLI entry (`turnpoint-purger-cli`) and reusable automation core.
- `turnpoint_purger_ui.py` – Tkinter GUI entry (`turnpoint-purger-gui`).
- `NDISBUDGETER.py` – Budget export helper (`turnpoint-budgeter`).
- `purger_state.py` – Shared state store for sequential universal IDs.
- `assets/` – Optional artwork bundled with the GUI build.
- `turnpoint_cli.spec` / `turnpoint_gui.spec` – PyInstaller specs for Win/macOS executables.
- `pyproject.toml` – Packaging metadata + entry point declarations.

The purge counter & universal-sequence state is persisted at:

```
~/.turnpoint_purger/purger_state.json
```

This file is created (and updated) automatically after each successful purge, so every run gets a unique universal prefix (100001, 100002, …) and the GUI/CLI can display how many clients have been processed so far.

## Local development

```bash
python -m venv .venv
source .venv/bin/activate      # or .venv\Scripts\activate on Windows
pip install --upgrade pip
pip install -e .
```

Entry points after an editable install:

- `turnpoint-purger-cli` – prompts for the TurnPoint client ID in the terminal.
- `turnpoint-purger-gui` – launches the cinematic desktop UI with logs/progress bars.
- `turnpoint-budgeter` – runs the budget CSV generator against Excel exports.

Environment variables (`TP_USERNAME`, `TP_PASSWORD`) are still loaded from `.env`.

## Building distributable wheels / sdists

```bash
python -m build         # requires `pip install build`
```

Artifacts land in `dist/` (`.whl` + `.tar.gz`). These are cross-platform and still require the host to have Chrome (or Chromium) available for Selenium.

## Creating standalone executables (PyInstaller)

Both Windows and macOS builds share the same spec files. Run the commands on the respective host OS:

```bash
pyinstaller turnpoint_cli.spec   # console/CLI build
pyinstaller turnpoint_gui.spec   # windowed GUI build
```

Or invoke the helper for one-liners:

```bash
python build.py --gui        # macOS GUI build -> dist/macos/TurnpointPurger.app
python build.py --cli        # CLI build -> dist/<platform>/TurnpointPurgerCLI
python build.py --gui --cli  # build everything for the current OS
```

Outputs now land inside platform-specific folders (`dist/macos`, `dist/windows`, `dist/linux`). Bundle the entire folder (or wrap it in an installer such as MSI/DMG). The GUI spec bundles everything in a single windowed executable; the CLI spec keeps the console for live logs.

> Note: PyInstaller builds should be executed from an activated virtual environment that already has the project installed (e.g., `pip install -e .`). Each platform must be built on its own OS for best compatibility.

## Updating / rebuilding

1. Edit any source file (e.g., tweak the UI, extend the scraper, etc.).
2. Bump the version in `pyproject.toml` if you plan to publish new wheels.
3. Re-run `python -m build` or the `pyinstaller ...` commands (or `python build.py ...`).
4. Optionally clean previous artifacts with `rm -rf build dist __pycache__` before rebuilding.

Because installers/executables are just snapshots, you can iterate freely: the source code remains editable, and rebuilds automatically pick up the latest changes.

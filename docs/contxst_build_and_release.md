# Build and Release Context

## Packaging strategy
The project supports both source-based usage and PyInstaller-based distributables.

## Python packaging metadata (`pyproject.toml`)
- Package name: `turnpoint-purger`
- Declared version: `3.0.1`
- Runtime dependencies:
  - `python-dotenv`
  - `selenium`
  - `pandas`
  - `openpyxl`
  - `pillow`

### Console scripts
- `turnpoint-purger-cli` -> `importcsv:main`
- `turnpoint-purger-gui` -> `turnpoint_purger_ui:launch_ui`
- `turnpoint-budgeter` -> `NDISBUDGETER:generate_budget_exports`

## PyInstaller build paths
Build wrapper (`build.py`) routes output by host OS:
- macOS -> `dist/macos`
- Windows -> `dist/windows`
- Linux -> `dist/linux`

Both GUI and CLI builds share a `build/` workpath.

## Spec files

### `turnpoint_gui.spec`
- Entry: `turnpoint_purger_ui.py`
- Bundles `assets/`
- Windowed executable (`console=False`)
- Includes hidden imports for PIL and runtime helpers
- Produces `.app` bundle metadata for macOS

### `turnpoint_cli.spec`
- Entry: `importcsv.py`
- Bundles `assets/`
- Console executable (`console=True`)
- Includes hidden import for `NDISBUDGETER`

## Build helper (`build.py`)
Capabilities:
- Detect/install PyInstaller if missing (`requirements-build.txt`).
- Build GUI and/or CLI target based on flags.

Common commands:
```bash
python build.py --gui
python build.py --cli
python build.py --gui --cli
```

## Build-time dependency file
`requirements-build.txt` currently pins:
- `pyinstaller>=6.4`

## Release operations checklist
1. Ensure environment and dependencies are current.
2. Verify runtime docs and version labels are aligned.
3. Run tests (`pytest`).
4. Build target artifacts with `build.py` or direct `pyinstaller`.
5. Smoke-test CLI and GUI binaries on host OS.
6. Package and distribute `dist/<platform>/...` outputs.

## Repository hygiene utility
`Declutter.py` can clear:
- `build/`
- `dist/`
- `turnpoint_purger.egg-info`
- all `__pycache__` directories
- all `.DS_Store` files

Dry run:
```bash
python Declutter.py --dry-run
```

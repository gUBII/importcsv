# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the TurnpointPurger GUI build.
Produces a windowed app (no console) with bundled artwork assets.
"""

from pathlib import Path
import os

block_cipher = None

_here = globals().get("__file__")
if _here:
    project_root = Path(_here).resolve().parent
else:
    project_root = Path.cwd()
assets_dir = project_root / "assets"
datas = []
if assets_dir.exists():
    datas.append((str(assets_dir), "assets"))

a = Analysis(
    ["turnpoint_purger_ui.py"],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "NDISBUDGETER",
        "importcsv",
        "PIL",
        "PIL.Image",
        "PIL.ImageTk",
        "PIL.ImageSequence",
        "PIL.ImageOps",
        "PIL.ImageDraw",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="TurnpointPurger",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

app = BUNDLE(
    exe,
    name="TurnpointPurger.app",
    icon=None,
    bundle_identifier="com.nexix.turnpointpurger",
)

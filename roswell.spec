# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build for the Roswell desktop app.

Two things this must get right that a bare `pyinstaller desktop.py` does not:

1. The frontend and the SQL schema are read from disk at runtime and are not
   importable modules, so PyInstaller cannot discover them. Without the datas
   entries below the app boots and then 404s on every page.
2. yfinance, uvicorn and google-genai all resolve pieces at runtime, so their
   imports are invisible to static analysis.
"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = [
    ("frontend", "frontend"),          # served by StaticFiles at runtime
    ("backend/db/schema.sql", "backend/db"),
    ("backend/data", "backend/data"),  # sp500.txt, curated macro releases
]
datas += collect_data_files("yfinance")
datas += collect_data_files("certifi")

hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("yfinance")
    + collect_submodules("google.genai")
    + ["anthropic", "pandas", "numpy"]
)

a = Analysis(
    ["desktop.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Test and plotting stacks pull in tkinter/matplotlib and add hundreds of
    # megabytes for code that never runs in the packaged app.
    excludes=["pytest", "matplotlib", "tkinter", "PyQt5", "PySide2", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Roswell",
    debug=False,
    strip=False,
    upx=False,
    console=False,
)

coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False, name="Roswell",
)

app = BUNDLE(
    coll,
    name="Roswell.app",
    bundle_identifier="local.roswell.terminal",
    info_plist={
        "NSHighResolutionCapable": True,
        # The app talks to Yahoo, FRED, Google and Anthropic over TLS only.
        "NSAppTransportSecurity": {"NSAllowsArbitraryLoads": False},
        "LSMinimumSystemVersion": "11.0",
    },
)

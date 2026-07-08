# -*- mode: python ; coding: utf-8 -*-
#
# Build-time dependency: pip install pyinstaller-hooks-contrib
# (bundles pyobjc's Objective-C bridge metadata for AppKit/Quartz/Foundation
# automatically - without it, the frozen binary fails to import AppKit).


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('sootsprite.png', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# Deliberately a bare onefile executable, not a .app bundle: double-clicking
# it from Finder will open a Terminal window (no LSUIElement/bundle metadata
# for LaunchServices to route around it) - accepted tradeoff for "one file".
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Russgeist',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX's self-decompressing stub is unsafe on macOS - it doesn't play well
    # with Apple's executable-page/code-signing enforcement and is a known
    # cause of exactly this kind of illegal-instruction crash at launch.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

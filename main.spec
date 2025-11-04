# -*- mode: python ; coding: utf-8 -*-
import platform

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('jellyfin_rpc.ini', '.'),
        ('images/icon.png', '.'),
        ('images/icon.ico', '.'),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Jellyfin RPC',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['images/icon.ico'],
)

if platform.system() == 'Darwin':
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name='Jellyfin_RPC',
    )
    app = BUNDLE(
        coll,
        name='Jellyfin RPC.app',
        icon='images/icon.icns',
        bundle_identifier=None,
    )

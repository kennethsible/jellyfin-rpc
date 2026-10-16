# -*- mode: python ; coding: utf-8 -*-
import os
import platform

from PyInstaller.utils.hooks import collect_data_files

match platform.system():
    case 'Windows':
        exe_name, coll_name = 'Jellyfin RPC', 'Jellyfin RPC'
    case 'Darwin':
        exe_name, coll_name = 'jellyfin-rpc', 'jellyfin-rpc'
    case _:
        exe_name, coll_name = 'jellyfin-rpc', 'Jellyfin RPC'

a = Analysis(
    ['src/jellyfin_rpc/__main__.py'],
    pathex=['src'],
    binaries=[],
    datas=[
        ('jellyfin_rpc.ini', '.'),
        ('images/assets', 'images/assets'),
        ('images/icons', 'images/icons'),
        *collect_data_files('certifi'),
        *collect_data_files('language_data'),
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
    [],
    exclude_binaries=True,
    name=exe_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=os.getenv('MACOS_ARCH'),
    codesign_identity=None,
    entitlements_file=None,
    icon=['images/icons/icon.ico'],
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=coll_name,
)

if platform.system() == 'Darwin':
    app = BUNDLE(
        coll,
        name='Jellyfin RPC.app',
        icon='images/icons/icon.icns',
        bundle_identifier='com.kennethsible.jellyfin-rpc',
        info_plist={
            'CFBundleName': 'Jellyfin RPC',
            'CFBundleDisplayName': 'Jellyfin RPC',
            'NSAppleEventsUsageDescription': 'Jellyfin RPC requires permission for "System Events" to run at startup.',
        },
    )

# -*- mode: python ; coding: utf-8 -*-
import os
import glob
import platform
from PyInstaller.utils.hooks import collect_data_files

extra_binaries = []
extra_datas = []

if platform.system() == 'Linux':
    typelib_dirs = [
        '/usr/lib/x86_64-linux-gnu/girepository-1.0',
        '/usr/lib/x86_64-linux-gnu/girepository-2.0',
    ]
    typelib_names = [
        'GLib-2.0', 'GObject-2.0', 'Gio-2.0', 'GioUnix-2.0',
        'Gtk-3.0', 'Gdk-3.0', 'GdkPixbuf-2.0', 'Atk-1.0',
        'Pango-1.0', 'HarfBuzz-0.0', 'cairo-1.0', 'freetype2-2.0',
        'xlib-2.0', 'GModule-2.0', 'GLibUnix-2.0',
        'AyatanaAppIndicator3-0.1', 'AppIndicator3-0.1',
        'DBus-1.0', 'DBusGLib-1.0',
    ]
    for typelib_dir in typelib_dirs:
        for name in typelib_names:
            for path in glob.glob(os.path.join(typelib_dir, f'{name}.typelib')):
                extra_datas.append((path, 'gi_typelibs'))

    lib_dirs = ['/usr/lib/x86_64-linux-gnu']
    lib_patterns = [
        'libayatana-appindicator3*',
        'libayatana-indicator3*',
        'libayatana-ido3*',
        'libappindicator3*',
    ]
    for lib_dir in lib_dirs:
        for pattern in lib_patterns:
            for path in glob.glob(os.path.join(lib_dir, pattern)):
                extra_binaries.append((path, '.'))

a = Analysis(
    ['src/jellyfin_rpc/app.py'],
    pathex=[],
    binaries=extra_binaries,
    datas=[
        ('jellyfin_rpc.ini', '.'),
        ('images/icon.png', '.'),
        ('images/icon.ico', '.'),
        *collect_data_files('certifi'),
        *collect_data_files('language_data'),
        *extra_datas,
    ],
    hiddenimports=[
        'gi',
        'gi.repository.GLib',
        'gi.repository.GObject',
        'gi.repository.Gio',
        'gi.repository.Gtk',
        'gi.repository.GdkPixbuf',
        'gi.repository.AyatanaAppIndicator3',
    ],
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
    target_arch=os.getenv('MACOS_ARCH'),
    codesign_identity=None,
    entitlements_file=None,
    icon=['images/icon.ico'],
)
if platform.system() == 'Darwin':
    coll = COLLECT(
        exe,
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

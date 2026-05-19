# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for The Dispatch client.
#
# Build:
#   pip install pyinstaller
#   cd client
#   pyinstaller client.spec
#
# Output: dist/TheDispatch.exe  (single-file, no console window)

block_cipher = None

a = Analysis(
    ['client.py'],
    pathex=['.'],
    binaries=[
        # Bundle SII_Decrypt.exe alongside the app
        ('SII_Decrypt.exe', '.'),
    ],
    datas=[
        ('.env.example', '.'),
    ],
    hiddenimports=[
        'pystray._win32',
        'PIL._tkinter_finder',
        'sounddevice',
        'soundfile',
    ],
    hookspath=[],
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
    name='TheDispatch',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,      # no console window — runs silently in tray
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon='../static/icon.ico',  # uncomment after converting logo.png to .ico
)

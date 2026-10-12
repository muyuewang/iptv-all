# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['tv_app.py'],
    pathex=[],
    binaries=[],
    datas=[('assets', 'assets'), ('../shared', 'shared'), ('mpv', 'mpv')],
    hiddenimports=['urllib.request', 'urllib.error', 'urllib.parse', 'importlib.util', 'ssl', 'http.server', 'json', 'base64', 'email.parser', 'email.message'],
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
    name='电视直播',
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
    icon=['app.ico'],
)

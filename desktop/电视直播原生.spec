# -*- mode: python ; coding: utf-8 -*-
# 纯净电视 · 桌面原生一体化版（tkinter + mpv 内嵌）
# 打包：pyinstaller 电视直播原生.spec
import os
import sys

# tcl/tk 数据目录由 PyInstaller 的 tkinter hook 自动收集为 _tcl_data / _tk_data，
# 运行时 hook 会自动设好 TCL_LIBRARY —— 无需手工 add-data（手工再加会重复打包约 1000 个文件）。
# 若某环境 hook 失效导致启动报 "Can't find a usable init.tcl"，再打开下面的兜底：
# _tcl_src = os.path.join(sys.base_prefix, "tcl")
# _tcl_datas = [(_tcl_src, "tcl")] if os.path.isdir(_tcl_src) else []
_tcl_datas = []

a = Analysis(
    ['native_app.py'],
    pathex=[],
    binaries=[],
    # ../shared 一份目录进去，core.py / wvt.py / wvt_builtin.json / web 全都在里面
    # WebView2Loader.dll 放 _MEIPASS 根目录 —— embed.py 的 _loader_path() 会到 sys._MEIPASS 下找
    datas=[('assets', 'assets'), ('../shared', 'shared'), ('mpv', 'mpv'),
           ('WebView2Loader.dll', '.'), ('app.ico', '.')] + _tcl_datas,
    hiddenimports=['urllib.request', 'urllib.error', 'urllib.parse',
                   'importlib.util', 'ssl', 'json', 'base64', 're', 'time',
                   'os', 'sys', 'threading', 'subprocess', 'socket', 'shutil',
                   'queue', 'hashlib', 'colorsys', 'io',
                   # cdp.py（官网源嗅探器）用到的
                   'struct', 'tempfile', 'random',
                   # embed.py（官网源内嵌浏览器）用到的
                   'ctypes', 'ctypes.wintypes', 'ctypes.util',
                   # 本工程内的几个本地模块（靠 import 闭包一般能收到，显式更稳）
                   'cdp', 'embed', 'winchrome',
                   'PIL.Image', 'PIL'],
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

@echo off
REM ==========================================================================
REM  纯净电视 · Windows 端构建（绿色单文件 exe，免安装）
REM  用法：双击本文件，或命令行 build_windows.bat
REM  产出：dist\电视直播.exe
REM  依赖：Python 3.8+ ；脚本会自动 pip 安装 pyinstaller 与 pillow
REM ==========================================================================
setlocal
cd /d "%~dp0"

echo [1/4] 检查依赖...
python -m pip install --quiet --upgrade pyinstaller pillow || goto :fail

echo [2/4] 生成图标...
if exist "app.ico" (
  echo     已存在 app.ico，跳过
) else (
  python gen\gen_icon.py || goto :fail
)

echo [3/4] 打包单文件 exe...
REM 共享核心 core.py 作为数据文件引入，它 import 的 stdlib 子模块
REM 必须用 --hidden-import 显式声明，否则冻结后 import 会失败。
python -m PyInstaller --noconfirm --clean ^
  --onefile --noconsole ^
  --name "电视直播" ^
  --icon "app.ico" ^
  --add-data "assets;assets" ^
  --add-data "..\shared;shared" ^
  --hidden-import urllib.request ^
  --hidden-import urllib.error ^
  --hidden-import urllib.parse ^
  --hidden-import importlib.util ^
  --hidden-import ssl ^
  --hidden-import http.server ^
  --hidden-import json ^
  --hidden-import base64 ^
  --hidden-import email.parser ^
  --hidden-import email.message ^
  tv_app.py || goto :fail

echo.
echo [4/4] == 完成: %cd%\dist\电视直播.exe ==
dir /b dist
goto :eof

:fail
echo.
echo !! 构建失败，请检查上面的错误信息（多半是未安装 Python 或网络不通）
exit /b 1

@echo off
REM ==========================================================================
REM  纯净电视 · Windows 端构建（绿色单文件 exe，免安装）
REM  用法：双击本文件，或命令行 build_windows.bat
REM  产出：dist\电视直播.exe
REM  依赖：Python 3.8+（需带 tkinter，Windows 官方 Python 默认带）
REM        脚本会自动 pip 安装 pyinstaller 与 pillow
REM        pillow 仅用于台标解码（PNG/JPG 全支持）；装不上也能构建，台标只认 PNG
REM
REM  ★ v1.8：原生一体化窗口 —— tkinter 主窗口 + mpv 内嵌（不再弹独立窗口）
REM  ★ v1.9：台标 / EPG 面板 / 画面比例 / 音量 / 搜索 + 设置页（域名/UA/缓冲）
REM ==========================================================================
setlocal
cd /d "%~dp0"

echo [1/5] 检查依赖...
python -c "import tkinter" >nul 2>&1 || (
  echo !! 当前 Python 缺少 tkinter，请用 Windows 官方 Python（python.org 下载）重试
  goto :fail
)
python -m pip install --quiet --upgrade pyinstaller pillow || goto :fail

echo [2/5] 生成图标...
if exist "app.ico" (
  echo     已存在 app.ico，跳过
) else (
  python gen\gen_icon.py || goto :fail
)

echo [3/5] 检查 mpv 是否已就位（desktop\mpv\mpv.exe）...
if exist "mpv\mpv.exe" (
  echo     已存在 mpv，跳过
) else (
  echo     !! 缺少 mpv\mpv.exe，请先复制 mpv.exe + d3dcompiler_43.dll 到 mpv\ 目录
  echo        或运行： winget install --id shinchiro.mpv -e 后手动复制
)

echo [4/5] 打包单文件 exe...
REM 共享核心 core.py 作为数据文件引入，它 import 的 stdlib 子模块
REM 必须用 --hidden-import 显式声明，否则冻结后 import 会失败。
REM mpv 目录（mpv.exe + d3dcompiler_43.dll）一并打入，别人电脑免安装即硬解。
python -m PyInstaller --noconfirm --clean 电视直播原生.spec || goto :fail

echo.
echo [5/5] == 完成: %cd%\dist\电视直播.exe ==
dir /b dist
goto :eof

:fail
echo.
echo !! 构建失败，请检查上面的错误信息（多半是未安装 Python 或网络不通）
exit /b 1

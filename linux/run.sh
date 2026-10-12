#!/usr/bin/env bash
# ============================================================================
#  纯净电视 · Linux 端 一键运行（源码方式，零编译）
#
#   用法：
#     ./run.sh              # 打开原生一体化窗口（tkinter + mpv 内嵌）
#     ./run.sh --browser    # 退回浏览器壳形态（tv_app.py，软解兜底）
#
#   依赖：python3 + python3-tk（sudo apt install python3-tk）+ mpv（sudo apt install mpv）
#         缺 mpv 时不报错，只提示 —— 无 mpv 无法硬解，建议安装。
# ============================================================================
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

# 1) 找 python3
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "!! 未找到 python3，请先安装：sudo apt install python3   （或 dnf/pacman 对应命令）"
  exit 1
fi
echo "使用: $($PY --version 2>&1)  ($(command -v $PY))"

# 2) 备用：浏览器壳形态
if [ "$1" = "--browser" ]; then
  shift
  if ! command -v google-chrome >/dev/null 2>&1 \
     && ! command -v chromium >/dev/null 2>&1 \
     && ! command -v chromium-browser >/dev/null 2>&1 \
     && ! command -v microsoft-edge >/dev/null 2>&1; then
    echo "提示: 未检测到 Chrome/Chromium/Edge，将以普通浏览器标签页打开。"
  fi
  exec "$PY" "$HERE/../desktop/tv_app.py" "$@"
fi

# 3) 原生一体化窗口：前置检查 tkinter 与 mpv
"$PY" -c "import tkinter" 2>/dev/null || {
  echo "!! 当前 Python 缺 tkinter，请安装：sudo apt install python3-tk"
  echo "   （或用 ./run.sh --browser 走浏览器软解形态）"
  exit 1
}
if ! command -v mpv >/dev/null 2>&1; then
  echo "提示: 未检测到 mpv —— 无法 GPU 硬解。建议： sudo apt install mpv"
fi

exec "$PY" "$HERE/../desktop/native_app.py" "$@"

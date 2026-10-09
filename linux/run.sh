#!/usr/bin/env bash
# ============================================================================
#  纯净电视 · Linux 端 一键运行（源码方式，零编译）
#
#   用法：
#     ./run.sh              # 起服务并弹出应用窗口
#     ./run.sh --noopen     # 只起服务，用浏览器手动访问（服务器/调试用）
#     ./run.sh --log        # 打开日志 tvlive.log
#
#   依赖：python3（系统自带即可）+ 任一 Chromium 系浏览器
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

# 2) 检查浏览器（仅提示，不阻塞 —— 程序内部还会退回默认浏览器打开标签页）
if ! command -v google-chrome >/dev/null 2>&1 \
   && ! command -v chromium >/dev/null 2>&1 \
   && ! command -v chromium-browser >/dev/null 2>&1 \
   && ! command -v microsoft-edge >/dev/null 2>&1; then
  echo "提示: 未检测到 Chrome/Chromium/Edge。"
  echo "      程序仍可运行，但会以普通浏览器标签页打开（没有独立窗口外观）。"
  echo "      建议安装： sudo apt install chromium-browser   （Debian/Ubuntu）"
  echo "                sudo dnf install chromium           （Fedora）"
fi

# 3) 启动（tv_app.py 与 Windows 端完全同一份代码，业务逻辑来自 ../shared/core.py）
exec "$PY" "$HERE/../desktop/tv_app.py" "$@"

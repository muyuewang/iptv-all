#!/usr/bin/env bash
# ============================================================================
#  纯净电视 · Linux 端 打包脚本
#
#   用法：
#     ./build_linux.sh              # 打包单文件可执行（PyInstaller）
#     ./build_linux.sh --appimage   # 额外做成 AppImage（需能联网装 appimagetool）
#
#   产出：
#     dist/纯净电视          单文件可执行（chmod +x 双击即跑）
#     dist/纯净电视-x86_64.AppImage    （可选）
# ============================================================================
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
DESK="$ROOT/desktop"
DIST="$HERE/dist"
BUILD="$HERE/build"

cd "$DESK"

# ---- 1) 依赖 ----
echo "== 1/4 检查 Python 与打包工具 =="
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null || { echo "!! 未找到 python3"; exit 1; }
"$PY" -m pip --version >/dev/null 2>&1 || {
  echo "!! 缺少 pip，请安装： sudo apt install python3-pip"; exit 1; }
# 虚拟环境避免污染系统 Python（PEP 668 的 externally-managed 环境必需）
if [ ! -d "$BUILD/venv" ]; then
  echo "    创建虚拟环境 $BUILD/venv ..."
  "$PY" -m venv "$BUILD/venv" || { echo "!! 创建 venv 失败（sudo apt install python3-venv）"; exit 1; }
fi
VPY="$BUILD/venv/bin/python"
"$VPY" -m pip install --quiet --upgrade pip
"$VPY" -m pip install --quiet --upgrade pyinstaller pillow

# ---- 2) 图标 ----
echo "== 2/4 准备图标 =="
if [ ! -f "$DESK/app.ico" ]; then
  "$VPY" "$DESK/gen/gen_icon.py" || true
fi
# 额外做一份 PNG（AppImage / 桌面快捷方式用）
"$VPY" - "$DESK/app.ico" "$HERE/app.png" <<'PYEOF' || true
import sys
try:
    from PIL import Image
    Image.open(sys.argv[1]).resize((256, 256)).save(sys.argv[2])
    print("app.png 已生成")
except Exception as e:
    print("生成 PNG 跳过:", e)
PYEOF

# ---- 3) 打包单文件 ----
echo "== 3/4 PyInstaller 打包 =="
rm -rf "$DESK/build" "$DESK/dist" "$DIST"
"$VPY" -m PyInstaller --noconfirm --clean \
  --onefile --console \
  --name "纯净电视" \
  --icon "$DESK/app.ico" \
  --add-data "$DESK/assets:assets" \
  --add-data "$ROOT/shared:shared" \
  --hidden-import urllib.request \
  --hidden-import urllib.error \
  --hidden-import urllib.parse \
  --hidden-import importlib.util \
  --hidden-import ssl \
  --hidden-import http.server \
  --hidden-import json \
  --hidden-import base64 \
  "$DESK/tv_app.py"

mkdir -p "$DIST"
cp "$DESK/dist/纯净电视" "$DIST/纯净电视"
chmod +x "$DIST/纯净电视"
echo "    -> $DIST/纯净电视"

# ---- 4) 可选：AppImage ----
if [ "$1" = "--appimage" ]; then
  echo "== 4/4 制作 AppImage =="
  APPDIR="$BUILD/AppDir"
  rm -rf "$APPDIR"
  mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/applications" \
           "$APPDIR/usr/share/icons/hicolor/256x256/apps"
  cp "$DIST/纯净电视" "$APPDIR/usr/bin/纯净电视"
  [ -f "$HERE/app.png" ] && cp "$HERE/app.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/cleantv.png"
  cat > "$APPDIR/cleantv.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=纯净电视
Name[en]=CleanTV Live
Comment=无广告电视直播
Exec=纯净电视
Icon=cleantv
Categories=AudioVideo;Video;TV;
Terminal=false
EOF
  cp "$APPDIR/cleantv.desktop" "$APPDIR/usr/share/applications/"
  [ -f "$HERE/app.png" ] && cp "$HERE/app.png" "$APPDIR/cleantv.png"

  # 下载 appimagetool（仅需一次）
  TOOL="$BUILD/appimagetool-x86_64.AppImage"
  if [ ! -f "$TOOL" ]; then
    echo "    下载 appimagetool ..."
    curl -L --fail -o "$TOOL" \
      https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage \
      || { echo "!! 下载失败，跳过 AppImage（单文件版仍可用）"; exit 0; }
    chmod +x "$TOOL"
  fi
  ARCH=x86_64 "$TOOL" --appimage-extract-and-run "$APPDIR" "$DIST/纯净电视-x86_64.AppImage" \
    || { echo "!! AppImage 制作失败（单文件版仍可用）"; exit 0; }
  chmod +x "$DIST/纯净电视-x86_64.AppImage"
  echo "    -> $DIST/纯净电视-x86_64.AppImage"
else
  echo "== 4/4 跳过 AppImage（如需请加 --appimage）=="
fi

echo
echo "== 完成 =="
ls -la "$DIST"

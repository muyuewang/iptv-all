#!/usr/bin/env bash
# ============================================================================
#  纯净电视 · Linux 端 安装到应用菜单（可选）
#
#   把程序装到 ~/.local（用户级，无需 root），之后即可在应用菜单里搜到「纯净电视」。
#   用法：
#     ./install.sh                 # 安装（源码方式，依赖 python3 常驻）
#     ./install.sh --uninstall     # 卸载
#     ./install.sh --appimage      # 安装 AppImage 版本（先跑 build_linux.sh --appimage）
# ============================================================================
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PREFIX="$HOME/.local"
APPS="$PREFIX/share/applications"
ICONS="$PREFIX/share/icons/hicolor/256x256/apps"
LAUNCHER="$PREFIX/bin/cleantv"

if [ "$1" = "--uninstall" ]; then
  rm -f "$APPS/cleantv.desktop" "$LAUNCHER" "$ICONS/cleantv.png"
  command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" 2>/dev/null || true
  echo "已卸载（保留 $HERE 下的文件）"
  exit 0
fi

mkdir -p "$APPS" "$ICONS" "$PREFIX/bin"

# 图标
if [ -f "$HERE/app.png" ]; then
  cp "$HERE/app.png" "$ICONS/cleantv.png"
elif [ -f "$ROOT/desktop/app.ico" ]; then
  python3 - "$ROOT/desktop/app.ico" "$ICONS/cleantv.png" <<'PYEOF' 2>/dev/null || true
import sys
from PIL import Image
Image.open(sys.argv[1]).resize((256, 256)).save(sys.argv[2])
PYEOF
fi

if [ "$1" = "--appimage" ]; then
  AI="$HERE/dist/纯净电视-x86_64.AppImage"
  [ -f "$AI" ] || { echo "!! 找不到 $AI，请先执行 ./build_linux.sh --appimage"; exit 1; }
  cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
exec "$AI" "\$@"
EOF
else
  # 启动器指向源码方式运行（用绝对路径保证菜单点击时工作目录正确）
  cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
exec "$HERE/run.sh" "\$@"
EOF
fi
chmod +x "$LAUNCHER"

cat > "$APPS/cleantv.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=纯净电视
Name[en]=CleanTV Live
Comment=无广告电视直播（接口直连，多线路自动切换）
Exec=cleantv
Icon=cleantv
Categories=AudioVideo;Video;TV;
Keywords=TV;live;iptv;电视;直播;
Terminal=false
StartupWMClass=crx__clean_tv
EOF

command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" 2>/dev/null || true
echo "安装完成：应用菜单里搜「纯净电视」，或终端执行 cleantv"
echo "卸载：$HERE/install.sh --uninstall"

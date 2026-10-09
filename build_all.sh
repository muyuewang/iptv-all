#!/usr/bin/env bash
# ============================================================================
#  纯净电视 · 三端一键构建
#
#   用法：
#     ./build_all.sh              # 当前系统能构建的都构建
#     ./build_all.sh android      # 只构建指定端（android|windows|linux）
#     ./build_all.sh --list       # 看各端可用状态
#
#   说明：Windows exe 必须在 Windows 上构建（PyInstaller 不支持交叉编译），
#         在 Linux 上跑本脚本会自动跳过 Windows 端并提示。
# ============================================================================
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
OS="$(uname -s 2>/dev/null || echo Windows)"

has() { command -v "$1" >/dev/null 2>&1; }

status() {
  echo "===================== 三端构建状态 ====================="
  echo "当前系统: $OS"
  echo
  echo "[安卓] android/"
  if [ -d "$ROOT/android/tools" ] || [ -n "${ANDROID_JAR:-}" ]; then
    echo "  工具链: 就绪    → bash android/build.sh"
  else
    echo "  工具链: 缺失（需 android/tools/，见 README）"
  fi
  echo
  echo "[Windows] desktop/build_windows.bat"
  case "$OS" in
    MINGW*|MSYS*|CYGWIN*) echo "  平台: 匹配    → cmd /c desktop/build_windows.bat" ;;
    *) echo "  平台: 不匹配（须在 Windows 上构建）" ;;
  esac
  echo
  echo "[Linux] linux/"
  if has python3; then
    echo "  python3: $($(command -v python3) --version 2>&1)    → ./linux/build_linux.sh"
  else
    echo "  python3: 缺失"
  fi
  echo "======================================================="
}

TARGET="${1:-all}"

if [ "$TARGET" = "--list" ]; then status; exit 0; fi

case "$TARGET" in
  android) DO_ANDROID=1; DO_WIN=0; DO_LINUX=0 ;;
  windows) DO_ANDROID=0; DO_WIN=1; DO_LINUX=0 ;;
  linux)   DO_ANDROID=0; DO_WIN=0; DO_LINUX=1 ;;
  all)     DO_ANDROID=1; DO_WIN=1; DO_LINUX=1 ;;
  *) echo "未知目标: $TARGET （可用: android|windows|linux|--list）"; exit 1 ;;
esac

FAILED=""

# ---------------- 安卓 ----------------
if [ "$DO_ANDROID" = 1 ]; then
  echo; echo "########## 构建安卓端 ##########"
  if [ -d "$ROOT/android/tools" ] || [ -n "${ANDROID_JAR:-}" ]; then
    ( cd "$ROOT/android" && bash build.sh ) || FAILED="$FAILED android"
  else
    echo "跳过：缺少 android/tools/ 构建工具链（见 README「安卓构建工具链从哪来」）"
    FAILED="$FAILED android(缺工具链)"
  fi
fi

# ---------------- Windows ----------------
if [ "$DO_WIN" = 1 ]; then
  echo; echo "########## 构建 Windows 端 ##########"
  case "$OS" in
    MINGW*|MSYS*|CYGWIN*)
      ( cd "$ROOT/desktop" && cmd //c build_windows.bat ) || FAILED="$FAILED windows"
      ;;
    *)
      echo "跳过：Windows exe 必须在 Windows 系统上构建（PyInstaller 不支持交叉编译）。"
      echo "      请在 Windows 上双击 desktop\\build_windows.bat。"
      ;;
  esac
fi

# ---------------- Linux ----------------
if [ "$DO_LINUX" = 1 ]; then
  echo; echo "########## 构建 Linux 端 ##########"
  case "$OS" in
    MINGW*|MSYS*|CYGWIN*)
      echo "跳过：当前是 Windows；Linux 端请在 Linux 机器上执行 ./linux/build_linux.sh"
      ;;
    *)
      ( cd "$ROOT/linux" && bash build_linux.sh ) || FAILED="$FAILED linux"
      ;;
  esac
fi

echo
if [ -n "$FAILED" ]; then
  echo "==== 完成，但以下端未成功：$FAILED ===="
  exit 1
fi
echo "==== 全部完成 ===="
status

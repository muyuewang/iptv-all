#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# adev.sh —— 安卓端 WebView 远程调试求值（一次调用内完成 adb 转发 + CDP 求值）
#
# 背景：本机 bash 每次调用结束后 adb server 会被回收，forward 也随之失效，
#       所以「start-server → forward → curl /json/list → CDP」必须挤在一次调用里。
#
# 用法：
#   bash tools/adev.sh "<js 表达式>"                 # 默认取我们自己的 player.html
#   bash tools/adev.sh "<js 表达式>" cctv            # 取 URL 里含 cctv 的目标（覆盖层 WebView）
#   ADEV_MATCH=webviewtv bash tools/adev.sh "1"      # 自定义匹配子串
# ---------------------------------------------------------------------------
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
PT="$HERE/../android/tools/platform-tools"
A="$PT/adb.exe"
PY="C:/Users/fengxin/.workbuddy/binaries/python/versions/3.13.12/python.exe"

EXPR="${1:-1}"
MATCH="${2:-${ADEV_MATCH:-file:}}"
# @overlay = 取「不是我们自己的 player.html」那个 target（即官方页面覆盖层）
if [ "$MATCH" = "@overlay" ]; then MATCH="!file:"; fi

"$A" start-server >/dev/null 2>&1
for i in 1 2 3 4 5; do
  STATE=$("$A" get-state 2>/dev/null | tr -d '\r')
  [ "$STATE" = "device" ] && break
  sleep 1
done

SOCK=$("$A" shell "cat /proc/net/unix" 2>/dev/null | tr -d '\r' \
       | grep -a -o "webview_devtools_remote_[0-9]*" | sort -u | head -1)
if [ -z "$SOCK" ]; then
  echo "[adev] 没找到 webview_devtools_remote socket（应用没跑？）" >&2
  exit 2
fi

"$A" forward tcp:9222 localabstract:"$SOCK" >/dev/null 2>&1

LIST=$(curl -s --max-time 8 http://127.0.0.1:9222/json/list)
if [ -z "$LIST" ]; then
  echo "[adev] /json/list 拿不到内容（forward 失败？sock=$SOCK）" >&2
  exit 3
fi

WS=$(printf '%s' "$LIST" | "$PY" -c "
import sys, json
m = sys.argv[1]
d = json.load(sys.stdin)
if m.startswith('!'):
    kw = m[1:]
    hit = [t for t in d if kw not in (t.get('url') or '')]
else:
    hit = [t for t in d if m in (t.get('url') or '')]
if not hit:
    hit = [t for t in d if t.get('type') == 'page']
if not hit:
    sys.exit(4)
print(hit[0]['webSocketDebuggerUrl'])
" "$MATCH")

if [ -z "$WS" ]; then
  echo "[adev] 没匹配到 target（match=$MATCH）；现有目标：" >&2
  printf '%s' "$LIST" | "$PY" -c "
import sys, json
for t in json.load(sys.stdin):
    print('   ', t.get('type'), t.get('url'))
" >&2
  exit 4
fi

echo "[adev] target=$WS" >&2
"$PY" "$HERE/android_eval.py" "$WS" "$EXPR"

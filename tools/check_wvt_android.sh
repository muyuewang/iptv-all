#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# check_wvt_android.sh —— 安卓端「官网源 · 网页线路」批量冒烟
#
# 逐个在某频道上打开官网直播页，等固定时间后回报：
#   最终落在哪条线路、video readyState / currentTime / 全屏矩形
#
# 用法：
#   bash tools/check_wvt_android.sh "广东卫视" "江苏卫视" "四川卫视"
#   WAIT=30 bash tools/check_wvt_android.sh "北京卫视"      # 自定义等待秒数
#
# 前置：模拟器/手机已连 adb，且 纯净电视 已安装并处于前台。
# ---------------------------------------------------------------------------
set -u
cd "$(cd "$(dirname "$0")/.." && pwd)"

WAIT="${WAIT:-28}"

PROBE='(function(){var v=document.getElementsByTagName("video")[0];var r=v?v.getBoundingClientRect():null;var e=document.getElementById("error_msg_player");return {url:location.href.split("?")[0],rs:v?v.readyState:-1,ct:v?Math.round(v.currentTime):-1,rect:r?[Math.round(r.left),Math.round(r.top),Math.round(r.width),Math.round(r.height)]:null,err:(e&&e.innerText.trim())||""};})()'

# 保证应用在前台并停在「官网源」标签
bash tools/adev.sh "(function(){if(typeof loadCat==='function'&&curCat!=='wvt')loadCat('wvt');return 1;})()" >/dev/null 2>&1
sleep 2

for name in "$@"; do
  echo "──────────────────────────────────────────────"
  echo "▸ $name"
  bash tools/adev.sh "(function(){openChannel('wvt','$name','$name');return 1;})()" >/dev/null 2>&1
  sleep "$WAIT"
  echo -n "  主界面索引: "
  bash tools/adev.sh "(function(){return (cur.idx+1)+'/'+cur.urls.length+'  '+(cur.urls[cur.idx]||'').slice(0,58);})()" 2>/dev/null
  echo -n "  页  面  态: "
  bash tools/adev.sh "$PROBE" @overlay 2>/dev/null
done

echo "──────────────────────────────────────────────"
echo "logcat（最近 25 行 [WVT]）："
PT="android/tools/platform-tools"
"$PT/adb.exe" start-server >/dev/null 2>&1
"$PT/adb.exe" logcat -d -s CleanTV 2>/dev/null | tr -d '\r' | grep -a "\[WVT\]" | tail -25

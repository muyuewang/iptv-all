#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# _probe_orient_flash.sh —— 「退出横屏播放时闪出网页播放器」复现 / 回归探针
#
# 现象（v2.9.1 已修）：手机端在横屏全屏看直播，按 BACK（一步退播放 → 回竖屏列表）
#   时，屏幕上会闪一下 Chromium 自带的播放器 —— 一个巨大的播放键 + 0:00 进度条。
#
# 两条成因（都要治）：
#   ① <video controls>            → 0:00 + 进度条 + 静音/全屏/更多按钮
#   ② <video> 无 poster 又无源    → Chromium 画默认海报：一个巨大的播放键
# 触发窗口：closePlayer() 里 nativeFreeze() 先撤 native-on/frame-on（#vwrap 由
#   hidden 变 visible），紧接着 v.removeAttribute("src"); v.load() 清空 <video> ——
#   这几百毫秒里浏览器那一套就画出来了，叠加旋转动画，肉眼必然可见。
#
# 用法：
#   MODE=dom  bash tools/_probe_orient_flash.sh     # 确定性：直接摆出那个 DOM 条件
#   MODE=back bash tools/_probe_orient_flash.sh     # 端到端：真实 BACK 路径 + 高频截图
#   TID=ws CID=7 NAME=广东卫视 / N=45 / SOFT=1  可覆盖
# 产出：_shots_orient/s01..sNN.png，配合 tools/_probe_orient_flash.py 出统计+拼图。
#
# 两个坑（都踩过）：
#   · 设备侧路径要关掉 Git Bash 的路径改写，但只能「按命令」加 MSYS_NO_PATHCONV，
#     不能 export 全局 —— 否则 adev.sh 里的 $HERE/android_eval.py 会被改写成
#     /c/... 而找不到；同理 pull 的本地目标要写成 Windows 路径。
#   · adev.sh 经 python 连 CDP，一次往返约 10s。绝不能用管道（| tail）接它的输出：
#     连接卡住时 timeout 只杀得掉 bash，后台 python 会一直占着 stdout → 永久阻塞。
# ---------------------------------------------------------------------------
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
A="$ROOT/android/tools/platform-tools/adb.exe"
OUT="$ROOT/_shots_orient"
DEV=/sdcard/_orient_flash
TID="${TID:-ws}"; CID="${CID:-7}"; NAME="${NAME:-广东卫视}"
N="${N:-45}"
SOFT="${SOFT:-0}"
MODE="${MODE:-back}"

adbx(){ MSYS_NO_PATHCONV=1 "$A" "$@"; }
AJ(){ timeout -k 5 "${AJ_T:-30}" bash "$HERE/adev.sh" "$1" > /tmp/_aj.out 2>&1
      grep -v '^\[adev\]' /tmp/_aj.out | tail -1; }

mkdir -p "$OUT"; rm -f "$OUT"/s*.png
adbx start-server >/dev/null 2>&1
# ★ 必须 force-stop 再起：否则只是把已经跑着的 Activity 拉到前台，
#   页面还是上一轮那份 DOM（手工改过 controls/poster 的话会得出错误结论）。
adbx shell am force-stop com.clean.tv >/dev/null 2>&1
sleep 1
adbx shell am start -n com.clean.tv/.MainActivity >/dev/null 2>&1
sleep 4

# ---------- 确定性模式：直接把 DOM 条件摆出来（与旋转/淡出时序无关） ----------
if [ "$MODE" = "dom" ]; then
  echo "[dom] 造「#player 显示 + #vwrap 可见 + <video> 无源」"
  AJ "(function(){try{if(window.setFsMode)setFsMode(false);}catch(e){}var p=document.getElementById('player');p.classList.remove('fs');p.classList.add('show');document.documentElement.classList.remove('frame-on','native-on');document.body.classList.remove('frame-on','native-on');var v=document.getElementById('v');try{v.pause();v.removeAttribute('src');v.load();}catch(e){}return JSON.stringify({controls:v.hasAttribute('controls'),poster:String(v.getAttribute('poster')||'').slice(0,24),vis:getComputedStyle(document.getElementById('vwrap')).visibility});})()"
  sleep 3
  adbx exec-out screencap -p > "$OUT/s01.png"
  echo "[dom] 截图 → $OUT/s01.png"
  exit 0
fi

# ---------- 端到端模式 ----------
if [ "$SOFT" = "1" ]; then PRE="NATIVE.ok=false; NATIVE.deadIdx={};"; else PRE="NATIVE.ok=true; NATIVE.deadIdx={};"; fi
echo "[1] 打开 $TID/$CID （SOFT=$SOFT）"
AJ "$PRE srcMode='local'; openChannel('$TID','$CID','$NAME');'go'"
sleep 12
echo "[2] 起播态:"
AJ "JSON.stringify({fs:isFsMode(),nat:NATIVE.active,vis:getComputedStyle(document.getElementById('vwrap')).visibility,on:document.body.className,nst:NATIVE.lastStats})"

echo "[3] 先开拍，再布防（★ adev 往返约 10s，若先布防再开拍，那段过渡会在开拍前跑完）"
adbx shell "rm -rf $DEV; mkdir -p $DEV" >/dev/null 2>&1
T0=$(date +%s)
adbx shell "i=0; while [ \$i -lt $N ]; do i=\$((i+1)); n=\$(printf '%02d' \$i); screencap -p $DEV/s\$n.png; done" >/dev/null 2>&1 &
SHOT=$!
sleep 2
AJ "setTimeout(function(){try{(function(){var p=document.getElementById('player');if(p.classList.contains('fs')&&window.setFsMode)window.setFsMode(false);document.getElementById('btnBack').click();})();}catch(e){}},3000);'armed'"
wait $SHOT
echo "   截图 $N 张，耗时 $(( $(date +%s)-T0 ))s"

echo "[4] 拉回本地（整目录一次 pull，避免逐文件循环）"
OUTW="$(cygpath -m "$OUT" 2>/dev/null || echo "$OUT")"
# ★ 别预先 mkdir 目标：adb pull 目录到「已存在的目录」会再套一层，
#   文件会落到 _pull/_orient_flash/ 里，看起来像「一张都没拉到」。
rm -rf "$OUT/_pull"
adbx pull "$DEV" "$OUTW/_pull" >/dev/null 2>&1
cp -f "$OUT"/_pull/s*.png "$OUT"/ 2>/dev/null
cp -f "$OUT"/_pull/_orient_flash/s*.png "$OUT"/ 2>/dev/null
rm -rf "$OUT/_pull"
adbx shell "rm -rf $DEV" >/dev/null 2>&1
echo "   共 $(ls "$OUT"/s*.png 2>/dev/null | wc -l) 张"

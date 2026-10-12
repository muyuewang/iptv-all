# -*- coding: utf-8 -*-
"""tvlab —— 安卓端真机（模拟器）回归的公共测试库。

为什么存在
----------
tools/ 下曾同时躺着 19 个脚本，每个都自带一份 CDP 客户端（WebSocket 握手 +
Runtime.evaluate + find_ws_url）与 adb 包装 —— 约 800~1000 行纯重复代码。
比行数更麻烦的是**判据各写各的**：排查「返回/后台声音残留」时就吃过两次亏
（`nativeStats()` 判活失效、`pidof -s` 把 pid 当进程名），都造成过假阴性。

这里收拢五类能力：

  · sh / shb        adb 包装：固定设备序列号、统一超时与文本解码
  · boot_app()      force-stop → 冷启动 → 等 devtools 就绪，直接返回 CDP
  · CDP             Runtime.evaluate / 截图，纯标准库实现的极简 WebSocket
  · 音频判据        tracks() / flinger_active() / started_rows() / is_playing()
  · cap() / key()   截图与按键
  · 起播探针        arm_native_probe() / wait_ready() / arm_start_timer() / start_ms()
  · 亮度采样        frame_mean() / bright_timeline()（判「切台到底黑多久」）

用法::

    from tvlab import boot_app, tracks, is_playing, cap, key, open_local, pick_channel

    cdp = boot_app()
    open_local(cdp)
    pick_channel(cdp, "广东")
    ...
    print(tracks())      # (started条数, flinger_active数, 明细行)

环境变量
--------
TV_SERIAL  目标设备（默认 emulator-5554）
TV_CDP_PORT 本地转发端口（默认 9334，多个脚本并行时各自换一个）
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
ADB_PATH = os.environ.get(
    "TV_ADB", os.path.join(_ROOT, "android", "tools", "platform-tools", "adb.exe"))
SERIAL = os.environ.get("TV_SERIAL", "emulator-5554")
PKG = "com.clean.tv"

_SHOT_DIR = os.path.join(_ROOT, "_shots_orient")


# --------------------------------------------------------------------------- adb

def adb_args(args):
    return [ADB_PATH, "-s", SERIAL] + list(args)


def sh(args, timeout=40):
    """跑一条 adb 命令，返回 stdout 文本（出错返回空串，不抛）。"""
    try:
        r = subprocess.run(adb_args(args), capture_output=True, text=True,
                           errors="ignore", timeout=timeout)
        return r.stdout or ""
    except Exception:
        return ""


def shb(args, timeout=90):
    """二进制输出（截图用）。"""
    try:
        r = subprocess.run(adb_args(args), capture_output=True, timeout=timeout)
        return r.stdout or b""
    except Exception:
        return b""


def key(code, wait=0.0):
    """发一个按键：key("HOME") / key("BACK") / key(4)。"""
    c = code if isinstance(code, int) else str(code)
    if isinstance(c, str) and not c.isdigit():
        c = "KEYCODE_" + c.upper()
    sh(["shell", "input", "keyevent", str(c)])
    if wait:
        time.sleep(wait)


def launch(wait=9.0):
    """冷启动 App（force-stop 后拉起），等界面稳定。"""
    sh(["shell", "am", "force-stop", PKG])
    time.sleep(1.5)
    sh(["shell", "am", "start", "-n", PKG + "/.MainActivity"])
    time.sleep(wait)


def foreground():
    """当前前台的 Activity（判「是否已退出 App」用）。"""
    out = sh(["shell", "dumpsys", "activity", "activities"])
    m = re.search(r"mResumedActivity.*?([\w.]+/[\w.]+)", out)
    return m.group(1) if m else "?"


# --------------------------------------------------------------------------- CDP

class _WS:
    """够用就好的 WebSocket 客户端（只用于 CDP，不做分片与压缩）。"""

    def __init__(self, url):
        m = re.match(r"ws://([^:/]+):(\d+)(/.*)", url)
        self.s = socket.create_connection((m.group(1), int(m.group(2))), timeout=10)
        k = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall(("GET %s HTTP/1.1\r\nHost: %s:%s\r\nUpgrade: websocket\r\n"
                        "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                        "Sec-WebSocket-Version: 13\r\n\r\n"
                        % (m.group(3), m.group(1), m.group(2), k)).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.s.recv(4096)
            if not chunk:
                raise EOFError("ws handshake failed")
            buf += chunk
        if b"101" not in buf.split(b"\r\n")[0]:
            raise EOFError("ws upgrade rejected: " + buf.split(b"\r\n")[0].decode("latin1"))

    def send(self, obj):
        data = json.dumps(obj).encode()
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            hdr = b"\x81" + bytes([0x80 | n])
        elif n < 65536:
            hdr = b"\x81" + bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr = b"\x81" + bytes([0x80 | 127]) + struct.pack(">Q", n)
        self.s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self, timeout=15):
        self.s.settimeout(timeout)
        hdr = self._recvn(2)
        n = hdr[1] & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._recvn(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._recvn(8))[0]
        if (hdr[0] & 0x0F) == 8:
            raise EOFError("ws closed")
        return self._recvn(n).decode("utf-8", "ignore")

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass

    def _recvn(self, n):
        buf = b""
        while len(buf) < n:
            c = self.s.recv(n - len(buf))
            if not c:
                raise EOFError("ws eof")
            buf += c
        return buf


class CDP:
    """页面侧求值。eval_js 出错返回 "EXC:..." / "TIMEOUT"，方便脚本直接打印。"""

    def __init__(self, ws_url):
        self.ws = _WS(ws_url)
        self.mid = 0

    def eval_js(self, expr, timeout=20):
        self.mid += 1
        mid = self.mid
        self.ws.send({"id": mid, "method": "Runtime.evaluate",
                      "params": {"expression": expr, "returnByValue": True,
                                 "awaitPromise": True}})
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                msg = json.loads(self.ws.recv(timeout=2))
            except (socket.timeout, TimeoutError):
                continue
            except EOFError:
                return "EXC:ws-closed"
            if msg.get("id") == mid:
                r = msg.get("result", {})
                if "exceptionDetails" in r:
                    return "EXC:" + json.dumps(r["exceptionDetails"])[:200]
                return r.get("result", {}).get("value")
        return "TIMEOUT"

    def json(self, expr, timeout=20):
        """求值并解析 JSON（失败返回 None）。"""
        r = self.eval_js(expr, timeout)
        try:
            return json.loads(r)
        except Exception:
            return None

    def close(self):
        self.ws.close()


def find_ws_url(port=None, match="player.html"):
    """从 /proc/net/unix 找 WebView 的 devtools socket，转发后取 ws 地址。

    ★ 必须是页面级 target；后缀是 WebView 沙箱进程号，≠ pidof 包名。
    ★ devtools 的 HTTP 请求必须带 Host: localhost，用 127.0.0.1 会被断连。
    """
    port = port or int(os.environ.get("TV_CDP_PORT", "9334"))
    out = sh(["shell", "cat", "/proc/net/unix"])
    socks = [l.split()[-1].lstrip("@") for l in out.splitlines()
             if "webview_devtools_remote" in l]
    for s in socks:
        sh(["forward", "tcp:%d" % port, "localabstract:" + s])
        try:
            c = http.client.HTTPConnection("localhost", port, timeout=6)
            c.request("GET", "/json", headers={"Host": "localhost:%d" % port})
            pages = json.loads(c.getresponse().read())
            for p in pages:
                if match in p.get("url", "") and p.get("type") == "page":
                    return p["webSocketDebuggerUrl"].replace("localhost", "127.0.0.1")
        except Exception:
            continue
    return None


def boot_app(wait=9.0, port=None, tries=6):
    """冷启动 App 并连上页面调试通道，返回 CDP 实例；失败返回 None。"""
    launch(wait)
    for _ in range(tries):
        url = find_ws_url(port)
        if url:
            try:
                return CDP(url)
            except Exception:
                pass
        time.sleep(1.5)
    return None


# --------------------------------------------------------------------------- 页面操作

def open_local(cdp, delay=3.0):
    """切到本机源并等列表渲染。"""
    cdp.eval_js("setSrc('local')")
    time.sleep(delay)


def open_wvt(cdp, delay=4.0):
    """切到官网源并等快照加载。"""
    cdp.eval_js("setSrc('wvt')")
    time.sleep(delay)


def pick_channel(cdp, kw, timeout=10):
    """按名字子串点开频道。返回 'open' / 'no-<kw>' / 'empty-list'。"""
    expr = ("(function(){var c=document.querySelectorAll('#list .ch');"
            "if(!c.length)return 'empty-list';"
            "for(var i=0;i<c.length;i++){if(c[i].textContent.indexOf(%s)>=0){"
            "c[i].click();return 'open';}}"
            "return 'no-%s';})()" % (json.dumps(kw), kw))
    return cdp.eval_js(expr, timeout)


def is_playing(cdp, timeout=8):
    """播放判据（修正版）：返回 vt*10+na。

    ★ 原生硬解通道下页面里没有 <video>.src，`nativeStats()` 从 JavaBridge
      线程读 player 还会抛 → 恒返回 {}。所以判活要看 NATIVE.active，
      不能只看 video.currentTime。
    """
    r = cdp.eval_js(
        "(function(){var v=document.getElementById('v');"
        "var vt=(v&&v.currentTime>0.2)?1:0;"
        "var na=(typeof NATIVE!=='undefined'&&NATIVE.active)?1:0;"
        "return vt*10+na;})()", timeout)
    try:
        return int(r)
    except Exception:
        return 0


def wait_playing(cdp, tries=18, gap=2.0, need_track=True, verbose=True):
    """等真正出声（page 判定 + 可选 dumpsys 音频轨佐证）。返回 bool。

    ★ 注意 NATIVE.active 是点击瞬间就置 true 的（表示「准备走原生通道」），
      所以 need_track=False 时这个判据很弱 —— 要验「真的出画了」请用 wait_ready()。
    """
    for t in range(tries):
        time.sleep(gap)
        p = is_playing(cdp)
        n, a, _ = tracks()
        if verbose:
            print("[%02d] play_flag=%d started=%d flinger_active=%d" % (t, p, n, a), flush=True)
        if p > 0 and (not need_track or n > 0 or a > 0):
            return True
    return False


# ----------------------------------------------------------------- 原生起播探针

_PROBE_JS = ("(function(){"
             "window.__lab=window.__lab||{state:'',ready:0,size:0,err:'',vsize:''};"
             "if(!window.__labWrapped){window.__labWrapped=1;var o=window.__onNativeState;"
             "window.__onNativeState=function(s,d){"
             "  try{ window.__lab.state=s;"
             "       if(s==='ready'){window.__lab.ready=1;}"
             "       /* ★ 只认「有真实宽高」的 size：media3 在切换媒体项时会先发一次"
             "          VideoSize.UNKNOWN(0x0)，把它当「起播」会造成 wait_ready 假阳性"
             "          （实测：真流还没出帧就被判成已起播）。 */"
             "       if(s==='size'){var m=/^(\\d+)x(\\d+)$/.exec(String(d||''));"
             "         if(m&&+m[1]>0&&+m[2]>0){window.__lab.size=1;window.__lab.vsize=String(d);}"
             "         else{window.__lab.vsize='0x0(ignored)';}}"
             "       if(s==='error'){window.__lab.err=String(d);}"
             "  }catch(e){}"
             "  if(o) return o(s,d);"
             "};}"
             "return 1;})()")


def arm_native_probe(cdp):
    """在页面里挂一层 __onNativeState 探针（记录 ready / size / error 时间线）。

    必须在点频道之前调用。'size' 来自 onVideoSizeChanged —— 说明解码器已经
    解析出视频轨、马上要出帧，是「真的起播了」最可靠的页面侧证据。
    """
    return cdp.eval_js(_PROBE_JS)


def native_probe(cdp):
    return cdp.json("JSON.stringify(window.__lab||{})") or {}


def reset_native_probe(cdp):
    """清掉上一次的 ready/size —— 换台前必须调用。

    否则上一台留下的 size=1 会让 wait_ready() 在第一轮就"通过"，
    把「继承认知」误当成「本台已起播」。（实测：A 台 1920x1080 会被 B 台
    在第 2 秒直接照抄通过。）包裹器 __labWrapped 保留，只清状态字段。
    """
    return cdp.eval_js(
        "(function(){window.__lab=window.__lab||{};"
        "window.__lab.state='';window.__lab.ready=0;window.__lab.size=0;"
        "window.__lab.err='';window.__lab.vsize='';return 1;})()")


def wait_ready(cdp, tries=22, gap=2.0, verbose=True):
    """等 ExoPlayer 报 ready，或拿到视频尺寸（解码器已出帧）。返回 bool。"""
    for t in range(tries):
        time.sleep(gap)
        p = native_probe(cdp)
        if verbose:
            print("  [%02d] state=%s ready=%s size=%s err=%s"
                  % (t, p.get("state"), p.get("ready"), p.get("vsize") or p.get("size"),
                     p.get("err") or "-"), flush=True)
        if p.get("ready") or p.get("size"):
            return True
    return False


# ------------------------------------------------------------- 起播计时（A/B 基准用）

_ST_JS = ("(function(){window.__st=window.__st||{t0:0,ready:0,ms:-1};"
          "if(!window.__stWrapped){window.__stWrapped=1;var o=window.__onNativeState;"
          "window.__onNativeState=function(s,d){"
          "  try{ if(s==='ready'&&window.__st.t0&&!window.__st.ready){"
          "        window.__st.ready=Date.now(); window.__st.ms=window.__st.ready-window.__st.t0;}"
          "  }catch(e){}"
          "  if(o) return o(s,d);"
          "};}"
          "return 1;})()")


def arm_start_timer(cdp):
    """再包一层 __onNativeState，量「mark_start() → 首次 ready」的毫秒数。

    必须在 arm_native_probe() 之后、点频道之前调用（它链在探针外层，
    保证探针自己的 state 记录先跑）。两个基准脚本共用这一段，
    省得各自维护一份探针。
    """
    return cdp.eval_js(_ST_JS)


def mark_start(cdp):
    """打起点。返回起点时间戳（页面时钟）。"""
    return cdp.eval_js("(function(){window.__st=window.__st||{};"
                       "window.__st.t0=Date.now();window.__st.ready=0;window.__st.ms=-1;"
                       "return window.__st.t0;})()")


def start_ms(cdp, timeout=45, gap=0.3):
    """等首次 ready，返回毫秒数；超时未 ready 返回 -1。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(gap)
        if cdp.eval_js("(window.__st&&window.__st.ready)?1:0", 6) == 1:
            break
    try:
        return int(cdp.eval_js("(window.__st&&window.__st.ms!==undefined)?window.__st.ms:-1", 8))
    except Exception:
        return -1


# --------------------------------------------------------------------------- 音频判据

def started_rows():
    """dumpsys audio 里 state:started 的条目（注意：对已死进程有滞后幽灵条目）。"""
    out = sh(["shell", "dumpsys", "audio"], timeout=25)
    return [l.strip()[:110] for l in out.splitlines()
            if "state:started" in l and l.strip().startswith("ID:")]


def flinger_active():
    """audio_flinger 里 active track 数。

    ★ active ≠ 一定出声（Server 地址推进才是）；但 active 归零 = 一定没在出声，
      用来判「残留是否清除」足够。
    """
    out = sh(["shell", "dumpsys", "media.audio_flinger"], timeout=25)
    m = re.search(r"(\d+) Tracks of which (\d+) are active", out)
    return int(m.group(2)) if m else -1


def tracks():
    """一次拿全：返回 (started 条数, flinger active 数, started 明细行)。"""
    rows = started_rows()
    return len(rows), flinger_active(), rows


def silence_check(cdp=None, rounds=12, gap=4.0, label="X", verbose=True):
    """静默采样：连续 rounds 次都 started=0 且 flinger_active=0 才算通过。"""
    ok = True
    for t in range(rounds):
        time.sleep(gap)
        n, a, rows = tracks()
        if verbose:
            print("[%s%02d +%ds] started=%d flinger_active=%d" % (label, t, int(gap), n, a), flush=True)
            for rr in rows:
                print("     ", rr, flush=True)
        if n != 0 or a != 0:
            ok = False
    return ok


# ------------------------------------------------------- toast 记录（判「有没有误杀」）

_TOAST_JS = ("(function(){window.__tw=window.__tw||[];"
             "if(!window.__twW){window.__twW=1;var o=window.toast;"
             "window.toast=function(t,x){try{window.__tw.push([Date.now(),String(t)]);}catch(e){}"
             " return o(t,x);};}"
             "window.__tw.length=0;return 1;})()")


def arm_toast_probe(cdp):
    """包裹页面里的 toast()，记录提示文案（调用时顺带清空历史）。

    用途：像「起播看门狗误杀正常线路」这类性质，除了提示文案没有别的可观测痕迹。
    数一下基线期间有没有出现过「起播超时」，比事后看截图猜测可靠得多。
    """
    return cdp.eval_js(_TOAST_JS)


def toast_log(cdp):
    """取回记录到的 toast 文案列表（[ts, text]）。"""
    return cdp.json("JSON.stringify(window.__tw||[])") or []


# --------------------------------------------------------------------------- 截图

def cap(name, shot_dir=None):
    """截图存到 _shots_orient/（gitignore 内），返回路径。"""
    d = shot_dir or _SHOT_DIR
    os.makedirs(d, exist_ok=True)
    raw = shb(["exec-out", "screencap", "-p"])
    if raw[:4] != b"\x89PNG":                 # 个别环境会把 \n 变成 \r\n
        raw = raw.replace(b"\r\n", b"\n")
    path = os.path.join(d, name)
    with open(path, "wb") as f:
        f.write(raw)
    print("   shot ->", name, flush=True)
    return path


def page_state(cdp, extra=""):
    """常用的一组页面状态（视口/播放层/列表/闸门），脚本里直接 print 出来最省事。"""
    expr = ("(function(){var p=document.getElementById('player'),l=document.getElementById('list');"
            "return JSON.stringify({iw:innerWidth,ih:innerHeight,"
            "so:(screen.orientation&&screen.orientation.type)||'',"
            "pcls:p?p.className:'',"
            "card:(document.getElementById('errCard')||{className:''}).className,"
            "toasts:document.querySelectorAll('#toasts .tst').length,"
            "listN:l?l.querySelectorAll('.ch').length:0,"
            "closed:typeof __sessionClosed!=='undefined'?__sessionClosed:'?',"
            "natActive:(typeof NATIVE!=='undefined'&&NATIVE.active)?1:0,"
            "frozen:(typeof NATIVE!=='undefined'&&NATIVE.frozen)?true:false,"
            "tune:typeof TUNE!=='undefined'?TUNE:'?'"
            + ("," + extra if extra else "") + "});})()")
    return cdp.json(expr) or {"raw": cdp.eval_js(expr)}


# --------------------------------------------------------------------- 亮度采样

def frame_mean(timeout=30):
    """整屏平均亮度 0~255（raw screencap，纯标准库，不依赖 Pillow）。失败返回 -1。

    为什么不用 -p（PNG）：PNG 得解压才能算均值。raw 格式是「头 + 裸像素」，
    直接按字节平均最省事。头有两种长度（12 字节 / 16 字节带 colorspace），
    用「剩余长度 == w*h*4」反过来判定，别硬编码。

    用途：量化「换台到底黑多久」—— 逐帧采亮度，找连续为 0 的最长区间，
    比「看起来没黑」这种主观判断可靠得多。
    """
    raw = shb(["exec-out", "screencap"], timeout=timeout)
    if len(raw) < 16:
        return -1
    w = int.from_bytes(raw[0:4], "little")
    h = int.from_bytes(raw[4:8], "little")
    if not (0 < w <= 4096 and 0 < h <= 4096):
        return -1
    off = 12
    if len(raw) - off != w * h * 4:
        off = 16
        if len(raw) - off != w * h * 4:
            return -1
    px = raw[off:off + w * h * 4]
    step = 4 * max(1, (w * h) // 20000)      # 固定 2 万次采样，别遍历整屏
    s = 0
    n = 0
    for i in range(0, len(px) - 3, step):
        s += px[i] + px[i + 1] + px[i + 2]
        n += 3
    return (s / n) if n else -1


def bright_timeline(sec=14.0, gap=0.28, floor=12.0, label="TL", verbose=True):
    """连续采样亮度，返回 (采样序列, 最长连续暗屏秒数)。

    floor：低于该均值算「暗屏」。实测三档：纯黑根视图 0；黑底 + 状态文字/转圈
    约 5~9（**这一档最容易骗过人** —— 你以为"有东西在动"其实完全没画面）；
    列表页深色底 25~45；视频画面 50~190。取 12 作阈值，才能把那层
    「黑底叠文字」判成暗屏。
    """
    t0 = time.time()
    seq = []
    while time.time() - t0 < sec:
        m = frame_mean()
        seq.append((round(time.time() - t0, 2), round(m, 1)))
        time.sleep(gap)
    worst = 0.0
    run = 0.0
    for _, m in seq:
        if m < floor:
            run += gap
            worst = max(worst, run)
        else:
            run = 0.0
    if verbose:
        print("[%s] 亮度序列: %s" % (label, " ".join("%.0f" % m for _, m in seq)), flush=True)
        print("[%s] 最长暗屏 ≈ %.2fs（阈值 %.0f，最低 %.0f）"
              % (label, worst, floor, min([m for _, m in seq] or [0])), flush=True)
    return seq, worst


def banner(title):
    print("=== " + title + " ===", flush=True)


class Verdict:
    """结算器：check(...) 累积，done() 统一打 PASS/FAIL 并给出退出码。"""

    def __init__(self, name=""):
        self.name = name
        self.fails = []
        self.checks = 0

    def check(self, cond, msg):
        self.checks += 1
        print("  [%s] %s" % ("PASS" if cond else "FAIL", msg), flush=True)
        if not cond:
            self.fails.append(msg)
        return bool(cond)

    def done(self):
        if self.fails:
            print("VERDICT: FAIL (%d/%d) -> %s" % (len(self.fails), self.checks, "; ".join(self.fails)),
                  flush=True)
            return 1
        print("VERDICT: PASS (%d/%d)" % (self.checks, self.checks), flush=True)
        return 0


if __name__ == "__main__":
    # 自检：不启动 App，只确认 adb 与设备可见
    print("ADB :", ADB_PATH, os.path.exists(ADB_PATH))
    print("设备:", SERIAL, "|", (sh(["shell", "getprop", "ro.build.version.release"]) or "?").strip(),
          "| webview:", (sh(["shell", "dumpsys", "package", PKG]) or "?").find("versionName") >= 0)
    print("前台:", foreground())
    print("音频:", tracks()[:2])
    sys.exit(0)

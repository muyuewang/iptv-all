# -*- coding: utf-8 -*-
"""
纯净电视 · 官网源嗅探器（Desktop only）
================================================================
WebViewTV 的「官网源」本质是**官方直播网页**，真正的播放地址藏在页面的
JS 播放器里。本模块用「真浏览器 + CDP」把它挖出来：

    启动 Edge/Chrome（带 --remote-debugging-port）
        -> 打开官方直播页
        -> CDP 监听 Network，捕获 .m3u8/.flv/.ts 等媒体请求
        -> 顺带记录该请求的 Referer / User-Agent / Cookie
        -> 拿到真实流地址后交给 mpv 硬解播放

为什么不能只抓 HTML：实测 20 个官网页面里只有 2 个在 HTML 里直接带 m3u8，
其余都是 JS 动态请求，必须真浏览器跑一遍。

依赖：纯标准库（自带最小 WebSocket 客户端，RFC6455 客户端侧）。
"""

import os
import re
import ssl
import json
import time
import base64
import struct
import socket
import random
import shutil
import tempfile
import subprocess
import urllib.request

IS_WIN = os.sys.platform.startswith("win")
IS_MAC = os.sys.platform == "darwin"

# ---------------------------------------------------------------- 媒体判定
MEDIA_HINT = (".m3u8", ".flv", ".ts", ".mp4", ".mkv", ".mpd", ".m4s")
# 统计/埋点类域名（这些 URL 里常带 playtime / 上报参数，要排除掉）
JUNK_HINT = ("p.data.", "/play.1.", "/collect", "/beacon", "/nlog", "/stat",
             "/log?", "/log.", "analytics", "/report", "/track", "doubleclick",
             "google-analytics", "/p.gif", "/1.gif", "/count", "hm.baidu")


def looks_like_media(url, mime=""):
    u = (url or "").lower().split("#")[0]
    if not u.startswith("http"):
        return False
    if any(h in u for h in MEDIA_HINT):
        return True
    m = (mime or "").lower()
    if m.startswith("video/") or m.startswith("audio/"):
        return True
    if "mpegurl" in m:
        return True
    return False


def media_score(url, mime=""):
    """
    给候选地址打分，分数越高越像「可以直接播的流」。

    为什么需要打分：官网页面里混着大量埋点请求（CCTV 的 p.data.cctv.com/play.1.3
    这类），如果「第一个命中就用」，会拿到 404 的统计地址。实测必须优先 m3u8。
    """
    u = (url or "").lower()
    m = (mime or "").lower()
    if not u.startswith("http"):
        return 0
    if any(j in u for j in JUNK_HINT):
        return 0
    if u.startswith("blob:") or u.startswith("data:"):
        return 0
    if ".m3u8" in u:
        return 100
    if "mpegurl" in m:
        return 95
    if m.startswith("video/") and "mp2t" not in m and "mp4" in m:
        return 80
    if ".mpd" in u or "dash" in m:
        return 70
    if ".flv" in u:
        return 65
    if ".mp4" in u:
        return 60
    if m.startswith("video/") or m.startswith("audio/"):
        return 50
    if u.endswith(".ts") or ".ts?" in u or "/hls/" in u:
        return 20          # 分片，能不用就不用
    return 0


# ================================================================ 极小 WS 客户端
class WSError(Exception):
    pass


class MiniWS:
    """
    RFC6455 客户端（Text 帧 + ping/pong + 分片重组），够驱动 CDP 即可。

    注意：DevTools 端点会拒绝带 Origin 头的握手，因此这里**不发 Origin**。
    """

    def __init__(self, url, timeout=30):
        m = re.match(r"ws://([^/:]+)(?::(\d+))?(/.*)?$", url)
        if not m:
            raise WSError("非法 ws 地址: " + url)
        host = m.group(1)
        port = int(m.group(2) or 80)
        path = m.group(3) or "/"
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(bytes(random.getrandbits(8) for _ in range(16))).decode()
        req = ("GET %s HTTP/1.1\r\n"
               "Host: %s:%d\r\n"
               "Upgrade: websocket\r\n"
               "Connection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n") % (path, host, port, key)
        self.sock.sendall(req.encode())
        self.buf = b""
        head = self._read_until(b"\r\n\r\n")
        if b" 101" not in head.split(b"\r\n")[0]:
            raise WSError("握手失败: " + head[:120].decode("latin1", "ignore"))
        self._mid = 0

    # ---------------------------------------------------------- 底层收发
    def _read_until(self, sep):
        while sep not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WSError("连接关闭")
            self.buf += chunk
        i = self.buf.index(sep) + len(sep)
        out, self.buf = self.buf[:i], self.buf[i:]
        return out

    def _read_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self.buf)))
            if not chunk:
                raise WSError("连接关闭")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text):
        payload = text.encode("utf-8")
        n = len(payload)
        hdr = bytearray([0x81])                       # FIN + text
        if n < 126:
            hdr.append(0x80 | n)
        elif n < (1 << 16):
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        mask = bytes(random.getrandbits(8) for _ in range(4))
        hdr += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(hdr) + masked)
        self._mid += 1
        return self._mid

    def recv(self):
        """收一条完整文本消息（自动处理分片 / 控制帧）。超时抛 socket.timeout。"""
        parts = []
        while True:
            b0, b1 = self._read_exact(2)
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read_exact(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else None
            data = self._read_exact(ln) if ln else b""
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x9:                         # ping -> pong
                self.sock.sendall(b"\x8a\x80" + b"\x00\x00\x00\x00")
                continue
            if opcode == 0xA:                         # pong
                continue
            if opcode == 0x8:                         # close
                raise WSError("对端关闭")
            parts.append(data)
            if fin:
                return b"".join(parts).decode("utf-8", "ignore")

    def call(self, method, params=None, wait=True, timeout=20):
        mid = self.send(json.dumps({"id": self._mid + 1, "method": method,
                                    "params": params or {}}))
        if not wait:
            return mid
        t0 = time.time()
        while time.time() - t0 < timeout:
            msg = json.loads(self.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise WSError(str(msg["error"]))
                return msg.get("result")
        raise WSError("CDP 超时: " + method)

    # ---------------------------------------------------------- 带事件泵的调用
    # 注意：CDP 的事件（Network.* 等）和命令响应混在同一条连接上。
    # 如果直接「发命令 -> 死等 id」，中间到达的事件会被丢掉，
    # 所以这里提供 send_call + pump 的组合，调用方自己把事件收好。
    def send_call(self, method, params=None):
        cid = self._mid + 1
        self.send(json.dumps({"id": cid, "method": method, "params": params or {}}))
        return cid

    def pump(self, timeout=1.0):
        """
        收一条消息。
          返回 ("event", msg) / ("resp", msg) / None(超时)
        """
        self.sock.settimeout(timeout)
        try:
            msg = json.loads(self.recv())
        except socket.timeout:
            return None
        except Exception:
            raise
        if "id" in msg:
            return ("resp", msg)
        return ("event", msg)

    def wait_result(self, cid, timeout=8, on_event=None):
        """等某个命令的响应；期间收到的事件交给 on_event 回调。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            got = self.pump(1.0)
            if got is None:
                continue
            kind, msg = got
            if kind == "event":
                if on_event:
                    on_event(msg)
                continue
            if msg.get("id") == cid:
                if "error" in msg:
                    raise WSError(str(msg["error"]))
                return msg.get("result")
        raise WSError("CDP 超时(等待响应)")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


# ================================================================ 浏览器
def find_browser():
    """找支持 --remote-debugging-port 的 Chromium 系浏览器。"""
    if IS_WIN:
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        lad = os.environ.get("LocalAppData", "")
        cands = [
            os.path.join(pf86, r"Microsoft\Edge\Application\msedge.exe"),
            os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe"),
            os.path.join(pf, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(pf86, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(lad, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(lad, r"Microsoft\Edge\Application\msedge.exe"),
        ]
    elif IS_MAC:
        cands = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                 "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                 "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    else:
        cands = []
        for n in ("google-chrome", "google-chrome-stable", "chromium",
                  "chromium-browser", "microsoft-edge", "microsoft-edge-stable"):
            p = shutil.which(n)
            if p:
                cands.append(p)
        cands += ["/usr/bin/google-chrome", "/usr/bin/chromium",
                  "/usr/bin/chromium-browser", "/snap/bin/chromium"]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def free_port(start=19333, tries=60):
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_BROWSER_PROCS = []


def launch_browser(url, port=None, offscreen=True, headless=False,
                   user_data_dir=None, extra_args=None):
    """
    启动浏览器并打开 url，返回 (proc, port, udd)。

    offscreen=True 时把窗口丢到屏幕外，避免嗅探过程打扰用户；
    拿到流之后调用方会把浏览器关掉。
    """
    exe = find_browser()
    if not exe:
        raise RuntimeError("未找到 Chrome/Edge，无法嗅探官网源")
    port = port or free_port()
    udd = user_data_dir or os.path.join(tempfile.gettempdir(), "cleantv_sniff_%d" % port)
    os.makedirs(udd, exist_ok=True)
    args = [
        exe,
        "--remote-debugging-port=%d" % port,
        "--user-data-dir=" + udd,
        "--no-first-run", "--no-default-browser-check",
        "--disable-sync", "--disable-breakpad", "--disable-component-update",
        "--no-service-autorun", "--password-store=basic",
        "--autoplay-policy=no-user-gesture-required",   # 免交互自动播放
        "--mute-audio",                                 # 嗅探阶段别出声
        # ★ 关键：窗口移到屏幕外时，Chromium 的「遮挡检测 / 后台节流」会把
        #   渲染和媒体播放降级甚至挂起，导致官网播放器根本不发流。
        #   下面这一组开关把它关掉，离屏也能正常起播。
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-background-timer-throttling",
        "--disable-ipc-flooding-protection",
        "--disable-features=Translate,MediaRouter,CalculateNativeWinOcclusion",
        "--window-size=1280,760",
    ]
    if headless:
        args.append("--headless=new")
    elif offscreen:
        args += ["--window-position=-32000,-32000"]
    args += (extra_args or [])
    args.append(url)
    flags = 0
    if IS_WIN:
        flags = 0x08000000                            # CREATE_NO_WINDOW（无控制台闪窗）
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, creationflags=flags)
    _BROWSER_PROCS.append((proc, udd))
    return proc, port, udd


def kill_browser(proc, udd=None, keep_profile=False):
    """
    关掉浏览器**整棵进程树**（Chromium 是多进程架构，只 terminate 主进程会留下
    一堆孤儿渲染进程），并清理临时 profile。
    """
    if proc and proc.poll() is None:
        pid = proc.pid
        try:
            if IS_WIN:
                # /T 连子孙一起杀，避免残留 msedge 渲染进程
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=15)
            else:
                import signal as _sig
                try:
                    os.killpg(os.getpgid(pid), _sig.SIGKILL)
                except Exception:
                    proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
    if udd and not keep_profile:
        try:
            shutil.rmtree(udd, ignore_errors=True)
        except Exception:
            pass


def _http_json(port, path, timeout=2):
    # ★ 端口为空时给个看得懂的错。之前直接 `"http://127.0.0.1:%d" % port`，
    #   port 是 None 就抛 TypeError('%d format: a real number is required, not NoneType')，
    #   一路冒到上层被记成「内嵌浏览器异常」，很难定位。
    if not port:
        raise RuntimeError("DevTools 端口无效：%r" % (port,))
    url = "http://127.0.0.1:%d%s" % (port, path)
    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(url, timeout=timeout, context=ctx) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def wait_targets(port, timeout=15):
    """等浏览器起来并返回页面 target 列表。"""
    if not port:
        raise RuntimeError("DevTools 端口无效：%r" % (port,))
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            ts = _http_json(port, "/json")
            pages = [t for t in ts if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
            if pages:
                return pages
        except Exception:
            pass
        time.sleep(0.25)
    raise RuntimeError("浏览器 DevTools 未就绪（端口 %d）" % port)


# ================================================================ 嗅探
_SNIFF_JS = r"""
(function(){
  if (window.__ctv_sniff) return 'already';
  window.__ctv_sniff = {hits: [], videos: []};
  function push(u, m){ try{ window.__ctv_sniff.hits.push([u, m||'']); }catch(e){} }
  // 1) 性能条目：已发出的媒体请求
  function scanPerf(){
    try{
      var es = performance.getEntriesByType('resource')||[];
      for (var i=0;i<es.length;i++){
        var u = es[i].name||'';
        if (/\.(m3u8|flv|ts|mp4|mkv|mpd)(\?|$)/i.test(u) || /m3u8|mpegurl/i.test(u)) push(u,'perf');
      }
    }catch(e){}
  }
  // 2) 包装 XHR / fetch，抓后续请求
  try{
    var _open = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function(m,u){ try{ push(u,'xhr'); }catch(e){} return _open.apply(this, arguments); };
    var _fetch = window.fetch;
    if (_fetch) window.fetch = function(a, b){
      try{ push(typeof a === 'string' ? a : (a && a.url), 'fetch'); }catch(e){}
      return _fetch.apply(this, arguments);
    };
  }catch(e){}
  // 3) 定时扫 <video>
  window.__ctv_timer = setInterval(function(){
    try{
      scanPerf();
      var vs = document.getElementsByTagName('video');
      for (var i=0;i<vs.length;i++){
        var v = vs[i];
        var src = v.currentSrc || v.src || '';
        if (src) push(src, 'video');
        if (!v.paused && v.readyState >= 2) window.__ctv_sniff.playing = true;
      }
      var ss = document.getElementsByTagName('source');
      for (var j=0;j<ss.length;j++){ if (ss[j].src) push(ss[j].src,'source'); }
    }catch(e){}
  }, 400);
  return 'installed';
})()
"""

_READ_JS = r"""
(function(){
  var s = window.__ctv_sniff || {hits:[]};
  var vids = [];
  try{
    var vs = document.getElementsByTagName('video');
    for (var i=0;i<vs.length;i++){
      vids.push({src: vs[i].currentSrc || vs[i].src || '', paused: vs[i].paused,
                 rs: vs[i].readyState, t: vs[i].currentTime});
    }
  }catch(e){}
  return JSON.stringify({hits: s.hits || [], videos: vids, playing: !!s.playing,
                         url: location.href, title: document.title});
})()
"""

# 很多官网播放器（央视网等）不会自己开播，必须替用户「点一下」。
# 这正是 WebViewTV 里 preClick / playButton 那套逻辑的通用版。
_KICK_JS = r"""
(function(){
  var out = [];
  var vs = document.getElementsByTagName('video');
  for (var i=0;i<vs.length;i++){
    var v = vs[i];
    try{ v.muted = false; v.defaultMuted = false; v.volume = 1; }catch(e){}
    try{
      var p = v.play();
      if (p && p.catch) p.catch(function(e){ window.__ctv_kickerr = e.name; });
      out.push('play');
    }catch(e){ out.push('playX'); }
  }
  // 常见播放按钮 / 大屏区域
  var sels = ['.player', '#player', '.video', '.vplayer', '.cntv-player',
              '[class*=btn-play]', '[class*=play-btn]', '.play-btn',
              '.play-button', '.vjs-big-play-button', '.prism-big-play-btn'];
  for (var s=0;s<sels.length;s++){
    var els = document.querySelectorAll(sels[s]);
    for (var k=0;k<els.length && k<3;k++){
      try{ els[k].click(); out.push('c:'+sels[s]); }catch(e){}
    }
  }
  // iframe 内的播放器也踹一脚（同源才可能成功）
  for (var f=0; f<window.frames.length; f++){
    try{
      var d = window.frames[f].document;
      var iv = d.getElementsByTagName('video');
      for (var j=0;j<iv.length;j++){ try{ iv[j].play(); out.push('iframe-play'); }catch(e){} }
    }catch(e){}
  }
  return out.join(',') || 'nothing';
})()
"""


MOBILE_UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")


class BrowserSession:
    """
    复用一个浏览器实例做多次嗅探。

    为什么必须复用：Edge/Chrome 冷启动（尤其是全新 profile 的首次初始化）
    实测要 40~80 秒，而真正抓流只要几秒。每次切台都重启浏览器完全不可接受，
    所以这里保持一个常驻实例，切台只是 Page.navigate。
    """

    def __init__(self, profile_dir=None, headless=False, offscreen=True, log=None):
        self.profile_dir = profile_dir
        self.headless = headless
        self.offscreen = offscreen
        self.log = log or (lambda *a: None)
        self.proc = None
        self.port = None
        self.udd = None
        self.ws = None
        self.ua = None
        self.nav_id = None

    def alive(self):
        try:
            return bool(self.proc and self.proc.poll() is None and self.ws)
        except Exception:
            return False

    def ensure(self, ua=None):
        """确保实例可用；UA 变化或进程已死则重建。"""
        if self.alive():
            if (ua or None) == (self.ua or None):
                return self
            self.log("  UA 变化，重建浏览器实例")
            self.close()
        self.ua = ua
        extra = []
        if ua == "MOBILE":
            extra.append("--user-agent=" + MOBILE_UA)
        elif ua and ua.startswith("Mozilla"):
            extra.append("--user-agent=" + ua)
        self.proc, self.port, self.udd = launch_browser(
            "about:blank", offscreen=self.offscreen, headless=self.headless,
            user_data_dir=self.profile_dir, extra_args=extra)
        try:
            pages = wait_targets(self.port, timeout=25)
            self.ws = MiniWS(pages[0]["webSocketDebuggerUrl"], timeout=45)
            self.ws.call("Network.enable", {})
            self.ws.call("Runtime.enable", {})
            self.ws.call("Page.enable", {})
            self.ws.call("Page.addScriptToEvaluateOnNewDocument",
                         {"source": _SNIFF_JS})
        except Exception:
            self.close()
            raise
        self.log("  浏览器实例就绪 (port=%s)" % self.port)
        return self

    def navigate(self, url):
        self.nav_id = self.ws.send_call("Page.navigate", {"url": url})

    def close(self):
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        self.ws = None
        # profile 目录保留（下次启动更快）
        kill_browser(self.proc, None)
        self.proc = self.port = self.udd = None


_DEFAULT_SESSION = None


def default_session(profile_dir=None, log=None, offscreen=True):
    """取全局共享浏览器实例（懒启动）。"""
    global _DEFAULT_SESSION
    if _DEFAULT_SESSION is None:
        _DEFAULT_SESSION = BrowserSession(profile_dir=profile_dir,
                                          log=log, offscreen=offscreen)
    return _DEFAULT_SESSION


def close_default_session():
    global _DEFAULT_SESSION
    if _DEFAULT_SESSION is not None:
        _DEFAULT_SESSION.close()
        _DEFAULT_SESSION = None


class Sniffer:
    """一次嗅探：导航到页面 -> 抓媒体地址。可复用外部传入的 BrowserSession。"""

    def __init__(self, page_url, port=None, headless=False, offscreen=True,
                 ua_mobile=False, timeout=28, log=None, session=None):
        self.page_url = page_url
        self.port = port
        self.headless = headless
        self.offscreen = offscreen
        self.ua_mobile = ua_mobile
        self.timeout = timeout
        self.log = log or (lambda *a: None)
        self.session = session or BrowserSession(
            headless=headless, offscreen=offscreen, log=self.log)
        self._owns_session = session is None
        self.ws = None

    # ---------------------------------------------------------- 生命周期
    def start(self):
        """
        复用（或新建）浏览器实例，把探针挂到「新文档」钩子上，再导航到目标页。
        这样才能保证页面一加载就开始记录网络请求，不漏掉首屏的 m3u8。
        """
        self.session.ensure(ua=("MOBILE" if self.ua_mobile else None))
        self.ws = self.session.ws
        self.session.navigate(self.page_url)
        return self

    def stop(self):
        if self._owns_session:
            self.session.close()
        self.ws = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *a):
        self.stop()

    # ---------------------------------------------------------- 采集
    def _offer(self, state, url, rid=None, mime=""):
        """把一个候选地址丢进打分池；返回当前最优分。"""
        if not url or url.startswith("blob:") or url.startswith("data:"):
            return state["best"]
        sc = media_score(url, mime)
        if sc <= 0:
            return state["best"]
        if sc > state["best"]:
            state["best"] = sc
            state["found"] = (url, rid)
            self.log("  候选 %.0f 分: %s" % (sc, url[:110]))
        return state["best"]

    def _on_net_event(self, msg, state):
        """处理一条网络事件，命中媒体地址就记进打分池。"""
        m = msg.get("method", "")
        p = msg.get("params", {}) or {}
        if m == "Network.requestWillBeSent":
            rid = p.get("requestId")
            r = p.get("request", {}) or {}
            u = r.get("url", "")
            state["urls"][rid] = u
            if media_score(u) > 0:
                state["hdrs"][rid] = dict(r.get("headers", {}) or {})
                self._offer(state, u, rid)
        elif m == "Network.responseReceived":
            resp = p.get("response", {}) or {}
            u = resp.get("url", "")
            rid = resp.get("requestId") or p.get("requestId")
            self._offer(state, u, rid, resp.get("mimeType", ""))
        elif m == "Network.requestWillBeSentExtraInfo":
            rid = p.get("requestId")
            if rid in state["urls"]:
                h = dict(state["hdrs"].get(rid, {}))
                h.update(p.get("headers", {}) or {})
                state["hdrs"][rid] = h

    def collect(self, deadline=None):
        """
        采集媒体地址：CDP 网络事件 + 页面内脚本轮询（双保险），按分数取最优。

        收敛策略：拿到 90 分以上（真 m3u8）立刻返回；否则继续找，
        直到超时或「已有低分候选后再等 settle 秒」。
        """
        t0 = time.time()
        deadline = deadline or (t0 + self.timeout)
        state = {"found": None, "best": 0, "hdrs": {}, "urls": {}}
        info = {}
        low_since = None
        settle = 6.0
        kicks = [0, 0]                    # 计划「启动播放」的时刻（相对秒）
        next_kick = 0
        kick_plan = [3.5, 8.0, 14.0, 22.0]

        while time.time() < deadline:
            el = time.time() - t0
            if next_kick < len(kick_plan) and el >= kick_plan[next_kick]:
                next_kick += 1
                try:
                    cid = self.ws.send_call("Runtime.evaluate", {
                        "expression": _KICK_JS, "returnByValue": True})
                    res = self.ws.wait_result(
                        cid, timeout=6,
                        on_event=lambda m: self._on_net_event(m, state))
                    self.log("  启动播放(%d): %s" % (next_kick,
                             (res or {}).get("result", {}).get("value")))
                except Exception as e:
                    self.log("  kick 失败", e)

            try:
                got = self.ws.pump(0.8)
            except Exception:
                got = None
            if got:
                kind, msg = got
                if kind == "event":
                    self._on_net_event(msg, state)

            try:
                cid = self.ws.send_call("Runtime.evaluate", {
                    "expression": _READ_JS, "returnByValue": True})
                res = self.ws.wait_result(
                    cid, timeout=5,
                    on_event=lambda m: self._on_net_event(m, state))
                raw = (res or {}).get("result", {}).get("value") or "{}"
                info = json.loads(raw)
                for u, _via in info.get("hits", []):
                    self._offer(state, u)
                for v in info.get("videos", []):
                    if v.get("src"):
                        self._offer(state, v["src"])
            except Exception as e:
                self.log("轮询失败", e)

            # 收敛判断
            if state["best"] >= 90:
                break
            if state["best"] > 0:
                if low_since is None:
                    low_since = time.time()
                elif time.time() - low_since > settle:
                    break
            time.sleep(0.2)

        if not state["found"]:
            return None

        url, rid = state["found"]
        hdrs = dict(state["hdrs"].get(rid, {})) if rid else {}
        low = {k.lower(): v for k, v in hdrs.items()}
        out_headers = {}
        for k in ("user-agent", "referer", "cookie", "origin", "authorization"):
            if low.get(k):
                out_headers[k] = low[k]
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            mm = re.match(r"(https?://[^/]+)", self.page_url)
            url = (mm.group(1) if mm else "") + url
        if not out_headers.get("referer"):
            out_headers["referer"] = self.page_url
        return {"url": url, "headers": out_headers, "via": "cdp",
                "page": info.get("url") or self.page_url,
                "title": info.get("title") or "",
                "playing": bool(info.get("playing"))}


def sniff_stream(page_url, timeout=28, headless=False, offscreen=True,
                 ua_mobile=False, log=None, session=None, profile_dir=None,
                 keep_session=True, retry_visible=True):
    """
    便捷入口：嗅探一个官方直播页，返回 {url, headers} 或 None。

    默认复用全局浏览器实例（keep_session=True）—— 第二次起只要几秒，
    首次会付出一次浏览器冷启动的成本。

    策略：先用「离屏窗口」试（不打扰用户）；失败再换「可见窗口」重试一次
    —— 少数站点在离屏/后台状态下会拒绝起播。
    """
    log = log or (lambda *a: None)
    if session is None and keep_session:
        session = default_session(profile_dir=profile_dir, log=log,
                                  offscreen=offscreen)

    plans = [(offscreen, headless)]
    if headless:
        plans = [(False, True), (False, False)]
    elif retry_visible:
        plans = [(True, False), (False, False)]

    for i, (off, hl) in enumerate(plans):
        if i:
            log("  首次未命中，改用可见窗口重试 ...")
        if session is not None and not off:
            # 复用实例是离屏的，可见窗口场景交给一次性实例
            use = None
        else:
            use = session
        s = Sniffer(page_url, headless=hl, offscreen=off,
                    ua_mobile=ua_mobile, timeout=timeout, log=log, session=use)
        try:
            s.start()
            r = s.collect()
            if r:
                return r
        except Exception as e:
            log("  嗅探异常(%s): %r" % ("visible" if not off else "offscreen", e))
        finally:
            s.stop()
    return None


if __name__ == "__main__":
    import sys
    u = sys.argv[1] if len(sys.argv) > 1 else "https://tv.cctv.com/live/cctv1/"
    headless = "--headless" in sys.argv
    print("嗅探:", u, "(headless=%s)" % headless)
    t = time.time()
    r = sniff_stream(u, headless=headless, log=print)
    print("耗时 %.1fs" % (time.time() - t))
    if r:
        print("命中:", r["url"][:140])
        print("请求头:", json.dumps(r["headers"], ensure_ascii=False)[:300])
    else:
        print("未命中")

"""
CDP 诊断：直接查官网直播页内 <video> 真实状态（2026-10-26）。

用途：区分「净化脚本副作用」vs「WebView2 直播窗环境不出画」。
- 若 readyState>=2 且 currentTime 在涨、videoWidth>0 → 视频确实在解码，
  画面灰/黑就是**渲染层**问题（净化脚本遮罩/z-index 干扰）。
- 若 readyState 卡 0、networkState 卡、或 error 非空 → 视频根本没拉到流，
  是**环境**问题（自动播放/窗口可见性/CDN 阻断）。

实现：手写 RFC6455 WebSocket 客户端（只用标准库，不依赖 websocket-client），
连接 CLEAN_TV_CDP_PORT 暴露的调试端口，枚举 target 找官网直播页，Runtime.evaluate
轮询读 video 状态。

用法：
  CLEAN_TV_CDP_PORT=9333 python tools/cvd_cdp_probe.py [--poll 30]
"""
import json
import os
import socket
import struct
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TV_RS = os.path.dirname(HERE)


def http_get(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def list_targets(port):
    return json.loads(http_get(f"http://127.0.0.1:{port}/json/list"))


def _wsmask(data):
    key = os.urandom(4)
    out = bytearray()
    n = len(data)
    out.append(0x81)
    if n < 126:
        out.append(0x80 | n)
    elif n < 65536:
        out.append(0x80 | 126)
        out += struct.pack(">H", n)
    else:
        out.append(0x80 | 127)
        out += struct.pack(">Q", n)
    out += key
    for i, b in enumerate(data):
        out.append(b ^ key[i % 4])
    return bytes(out)


def _wsrecv(sock, timeout=10):
    sock.settimeout(timeout)
    hdr = b""
    while len(hdr) < 2:
        try:
            chunk = sock.recv(2 - len(hdr))
        except socket.timeout:
            return None
        if not chunk:
            return None
        hdr += chunk
    if len(hdr) < 2:
        return None
    op = hdr[0] & 0x0F
    ln = hdr[1] & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", sock.recv(2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", sock.recv(8))[0]
    masked = hdr[1] & 0x80
    key = sock.recv(4) if masked else b""
    data = b""
    while len(data) < ln:
        chunk = sock.recv(ln - len(data))
        if not chunk:
            break
        data += chunk
    if masked:
        data = bytes(b ^ key[i % 4] for i, b in enumerate(data))
    return op, data


class CDP:
    def __init__(self, ws_url):
        self.ws_url = ws_url
        self._id = 0
        self.sock = None
        # ws_url 形如 ws://127.0.0.1:9333/devtools/page/xxx
        rest = ws_url[len("ws://"):]
        self.host, _, tail = rest.partition(":")
        self.port = int(tail.split("/")[0])
        self.path = "/" + tail.split("/", 1)[1]

    def connect(self):
        s = socket.create_connection((self.host, self.port), timeout=5)
        path = self.path
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        s.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += s.recv(1024)
        if b"101" not in resp.split(b"\r\n")[0]:
            raise RuntimeError(f"WS 握手失败: {resp[:200]}")
        self.sock = s

    def call(self, method, params=None, timeout=10):
        self._id += 1
        msg = {"id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        self.sock.sendall(_wsmask(json.dumps(msg).encode()))
        while True:
            r = _wsrecv(self.sock, timeout)
            if r is None:
                return None
            op, data = r
            if op == 1:
                continue  # ping
            if op == 8:
                return None
            if op == 2:  # binary
                continue
            try:
                obj = json.loads(data.decode("utf-8", "replace"))
            except Exception:
                continue
            if obj.get("id") == self._id:
                return obj
            # id 不匹配（事件或乱序响应）继续等，但防止死等：打印提示
            if "id" not in obj:
                # 事件通知，忽略
                continue

    def eval(self, expr):
        r = self.call(
            "Runtime.evaluate",
            {"expression": expr, "returnByValue": True, "awaitPromise": True},
        )
        if not r:
            return None
        res = r.get("result", {}).get("result", {})
        if res.get("type") != "object":
            return res.get("value")
        return res.get("value")


JS_STATE = r"""
(function(){
  var v = document.querySelector('video');
  if(!v) return {hasVideo:false, vCount:0};
  var vs = document.querySelectorAll('video');
  var out = {
    hasVideo:true, vCount:vs.length,
    readyState:v.readyState, networkState:v.networkState,
    paused:v.paused, ended:v.ended, muted:v.muted,
    currentTime:v.currentTime, duration:v.duration,
    videoWidth:v.videoWidth, videoHeight:v.videoHeight,
    seekable:(function(){try{return v.seekable.length?[v.seekable.start(0),v.seekable.end(0)]:[]}catch(e){return []}})(),
    buffered:(function(){try{return v.buffered.length?[v.buffered.start(0),v.buffered.end(0)]:[]}catch(e){return []}})(),
    error: v.error ? (v.error.code+'/'+v.error.message) : null,
    src: (v.currentSrc||v.src||'').slice(0,200),
    shade:(document.getElementById('__pure_video_shade')?true:false),
    pureFlag: window.__pureVideo===true,
    css:{objectFit:getComputedStyle(v).objectFit, position:getComputedStyle(v).position, zIndex:getComputedStyle(v).zIndex}
  };
  return out;
})()
"""


def main():
    port = os.environ.get("CLEAN_TV_CDP_PORT", "9333")
    poll = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].startswith("--poll") else (
        int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[1] == "--poll" else 30)

    # 找官网直播页 target
    targets = list_targets(port)
    live = None
    for t in targets:
        if t.get("type") == "page" and ("cctv" in t.get("url", "") or "yangshipin" in t.get("url", "") or "cctvnews" in t.get("url", "")):
            live = t
            break
    if not live:
        # 兜底：打印所有 page target 的 URL 供排查
        print("未找到官网直播页 target，现有 page target：")
        for t in targets:
            if t.get("type") == "page":
                print("  ", t.get("title"), "->", t.get("url", "")[:120])
        return 2

    print(f"目标页面: {live['title']} -> {live['url']}")
    cdp = CDP(live["webSocketDebuggerUrl"])
    cdp.connect()
    print("已连接 CDP，开始轮询 video 状态…\n")

    for i in range(poll):
        st = cdp.eval(JS_STATE)
        if st is None:
            print(f"[{i:02d}] eval 无返回")
        elif not st.get("hasVideo"):
            print(f"[{i:02d}] 尚无 <video>（vCount={st.get('vCount',0)}）")
        else:
            rs = ["EMPTY", "METADATA", "CURRENT", "FUTURE", "ENOUGH"][st["readyState"]] if st["readyState"] < 5 else "ENOUGH"
            ns = ["EMPTY", "IDLE", "LOADING", "NO_SOURCE"][st["networkState"]] if st["networkState"] < 4 else "?"
            print(
                f"[{i:02d}] readyState={st['readyState']}({rs}) networkState={st['networkState']}({ns}) "
                f"paused={st['paused']} muted={st['muted']} t={st['currentTime']:.2f}s "
                f"dim={st['videoWidth']}x{st['videoHeight']} buf={st['buffered']} err={st['error']}"
            )
            if i == 0:
                print(f"       src={st['src']}")
                print(f"       shade={st['shade']} pureFlag={st['pureFlag']} css={st['css']}")
        time.sleep(2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
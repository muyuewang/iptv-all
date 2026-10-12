# -*- coding: utf-8 -*-
"""HLS 切片容错探针：一个分片坏掉，会不会让整条线路作废？

背景
    `借鉴方案-TVBoxOS.md` 的 2.3 / 4.2 抄了 TVBoxOS 的 `HlsErrorHandlingPolicy`，
    但那份实现的语义**存疑**：它 `getFallbackSelectionFor` 返回 null，注释写「ExoPlayer
    会跳过这个切片继续播放」—— 而在 ExoPlayer 里返回 null 的语义是「不做 fallback」，
    `DefaultLoadErrorHandlingPolicy` 默认反而会做 track fallback（换轨道绕过坏数据）。
    两边的效果可能是**反的**。所以先实测，再决定抄不抄。

造场景
    本机起一个 HTTP 服务，提供 6 个真实 TS 分片（Apple 的 bipbop 测试流），
    但把第 3 个分片指到一个恒定 404 的地址 —— 这就是「源站吐了一个坏片」。
    经 `adb reverse` 映射进设备，再 CDP 注入让 ExoPlayer 直接播这个流。

看什么
    1. 服务端收到的**请求序列**：坏片被重试了几次？之后有没有继续请求 seg3？
    2. 播放器的 position 有没有越过坏片继续推进（说明真的跳过去了）
    3. 有没有报 error / 触发换线（说明整条线路作废）

结论（2026-10-11 实测）
    · **基线**：`/seg0 200 → /seg1 200 → /bad 404 ×6 → InvalidResponseCodeException
      → 整条线路作废`（position 停在 11883ms，正是前两个分片的 12 秒）。
      「一个分片坏掉 = 一条线路废掉」属实。
    · **照抄 TVBoxOS 的 HlsErrorHandlingPolicy 反而更差**：重试次数 6 → **22**，
      仍然 fatal。原因是它 `getFallbackSelectionFor` 返回 null 的语义是「**禁用**
      fallback」，与它注释里写的「跳过这个切片」正好相反。→ 该实现已回退。
    · 真正的解法在**错误处理层**（跳到直播边缘重播同流），见
      `tools/_probe_hls_live.py`（那个必须用**直播**流 —— VOD 的
      `seekToDefaultPosition()` 是回开头，跳不出坏片）。
"""
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tvlab import (ADB_PATH, PKG, SERIAL, arm_toast_probe, banner,  # noqa: E402
                   boot_app, cap, open_local, page_state, sh, toast_log)

PORT = 19556
CDN = ("https://devstreaming-cdn.apple.com/videos/streaming/examples/"
       "img_bipbop_adv_example_ts/v5/")
NSEG = 6          # 只取前 6 个分片（36s 内容），够看行为了
BAD_AT = 2        # 第 3 个分片坏掉

SEGS = {}         # index -> bytes
HITS = []         # (path, status) 服务端收到的请求序列
_CTX = ssl._create_unverified_context()


def adb(*a, timeout=40):
    return subprocess.run([ADB_PATH, "-s", SERIAL] + list(a),
                          capture_output=True, text=True, timeout=timeout)


# --------------------------------------------------------------------- 素材
def fetch_segments():
    banner("0) 预下载 %d 个真实 TS 分片（Apple bipbop 测试流）" % NSEG)
    t0 = time.time()
    for i in range(NSEG):
        u = CDN + "fileSequence%d.ts" % i
        for attempt in range(3):
            try:
                r = urllib.request.urlopen(u, context=_CTX, timeout=30)
                SEGS[i] = r.read()
                break
            except Exception as e:
                if attempt == 2:
                    print("  分片 %d 下载失败: %s" % (i, e), flush=True)
                    return False
                time.sleep(1.5)
        print("  seg%d %d KB" % (i, len(SEGS[i]) // 1024), flush=True)
    print("  共 %.1f MB · 耗时 %.1fs" % (sum(len(v) for v in SEGS.values()) / 1048576.0,
                                        time.time() - t0), flush=True)
    return len(SEGS) == NSEG


def build_playlist():
    L = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:6",
         "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD"]
    for i in range(NSEG):
        L.append("#EXTINF:6.00000,")
        # ★ 第 BAD_AT 个分片指到 404 黑洞
        L.append("http://127.0.0.1:%d/bad.ts" % PORT if i == BAD_AT
                 else "http://127.0.0.1:%d/seg%d.ts" % (PORT, i))
    L.append("#EXT-X-ENDLIST")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------- 服务端
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="application/octet-stream"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/test.m3u8":
            b = build_playlist().encode()
            HITS.append(("/test.m3u8", 200))
            self._send(200, b, "application/vnd.apple.mpegurl")
            return
        if p == "/bad.ts":
            HITS.append(("/bad.ts", 404))
            self._send(404)
            return
        m = re.match(r"^/seg(\d+)\.ts$", p)
        if m and int(m.group(1)) in SEGS:
            b = SEGS[int(m.group(1))]
            HITS.append((p, 200))
            self._send(200, b, "video/mp2t")
            return
        HITS.append((p, 404))
        self._send(404)


def start_server():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# --------------------------------------------------------------------- 采样
SAMPLE_JS = ("(function(){try{var s={};"
             "s.idx=(typeof cur!=='undefined')?cur.idx:-1;"
             "s.nat=NATIVE.active?1:0;s.ff=NATIVE.firstFrame?1:0;"
             "s.dead=NATIVE.dead?1:0;s.pos=-1;"
             "try{s.pos=JSON.parse(NativeTV.nativeStats()).pos;}catch(e){}"
             "return JSON.stringify(s);}catch(e){return '{\"e\":\"'+e+'\"}';}})()")


def sample(cdp, sec=30.0, gap=1.5):
    out = []
    t0 = time.time()
    while time.time() - t0 < sec:
        try:
            raw = cdp.eval_js(SAMPLE_JS, 8)
            s = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception as e:
            s = {"e": str(e)[:40]}
        s["t"] = round(time.time() - t0, 1)
        out.append(s)
    return out


def main():
    if not fetch_segments():
        print("素材不全，退出")
        return 2
    srv = start_server()
    print("本机 HTTP 已就绪 :%d · playlist=/test.m3u8（第 %d 个分片指向 /bad.ts=404）"
          % (PORT, BAD_AT + 1), flush=True)

    print("\nplaylist 预览：")
    for line in build_playlist().splitlines():
        print("   " + line, flush=True)

    banner("1) 映射端口并启动 App")
    adb("reverse", "tcp:%d" % PORT, "tcp:%d" % PORT)
    r = adb("reverse", "--list")
    print("adb reverse:", (r.stdout or "").strip().replace("\n", " | "), flush=True)
    cdp = boot_app()
    open_local(cdp)
    arm_toast_probe(cdp)

    banner("2) 注入测试流并播放（绕过频道接口，直接给 ExoPlayer 一个 HLS）")
    url = "http://127.0.0.1:%d/test.m3u8" % PORT
    js = ("(function(){try{"
          "cur.tid='local';cur.idx=0;cur.urls=[%r];cur.epg=[];"
          "if(typeof showPlayerLayer==='function')showPlayerLayer();"
          "playIdx(0);return 1;}catch(e){return 'err:'+e;}})()" % url)
    print("inject:", cdp.eval_js(js, 10), flush=True)

    banner("3) 采样 30s")
    rows = sample(cdp, 30.0, 1.5)
    prev = None
    for s in rows:
        line = ("[%5.1fs] idx=%s nat=%s ff=%s dead=%s pos=%s %s"
                % (s.get("t", 0), s.get("idx"), s.get("nat"), s.get("ff"),
                   s.get("dead"), s.get("pos"), s.get("e", "")))
        print(line, flush=True)
        prev = s
    cap("hls_probe.png")

    banner("4) 服务端请求序列（关键证据）")
    for p, st in HITS:
        print("   %-12s %s" % (p, st), flush=True)
    bad_hits = [p for p, st in HITS if p == "/bad.ts"]
    after_bad = 0
    seen_bad = False
    for p, st in HITS:
        if p == "/bad.ts":
            seen_bad = True
        elif seen_bad and p.startswith("/seg"):
            after_bad += 1
    print("\n  /bad.ts 被请求 %d 次 · 坏片之后又请求了 %d 个正常分片"
          % (len(bad_hits), after_bad), flush=True)

    banner("5) 结论")
    pos_max = max([s.get("pos", -1) for s in rows] or [-1])
    moved = pos_max > 0
    print("  position 最大值 %s（>0 = 真在播）" % pos_max, flush=True)
    texts = [t for _, t in toast_log(cdp)]
    print("  toast:", texts, flush=True)
    if after_bad >= 2 and moved:
        print("  => 跳过了坏片，继续播后面的分片（容错生效）", flush=True)
    elif seen_bad and after_bad == 0:
        print("  => 卡在坏片上：坏片之后再没请求正常分片（整条线路作废）", flush=True)
    else:
        print("  => 行为不明确，需看上面时间线", flush=True)

    banner("6) logcat（ExoPlayer 侧）")
    lg = sh(["logcat", "-d", "-v", "time"], timeout=40) or ""
    keep = [l for l in lg.splitlines()
            if any(k in l for k in ("ExoPlayer", "Hls", "Media3PlayerBackend",
                                    "LoadError", "chunk", "404"))]
    for l in keep[-25:]:
        print("   " + l.strip()[:150], flush=True)

    adb("reverse", "--remove", "tcp:%d" % PORT)
    srv.shutdown()
    print("\nPKG=%s （探针结束）" % PKG, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

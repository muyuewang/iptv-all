# -*- coding: utf-8 -*-
"""HLS **直播**坏片自救探针（v2.7）：验证「跳到直播边缘重播同一条流」真的能跳过坏片。

为什么不能只用 `_probe_hls.py`（VOD）
    VOD 里 `seekToDefaultPosition()` 是「回到开头」，跳不出坏片；而直播里它是
    「跳到最新可播位置」—— 这才是自救动作真正的语义。所以要造一个**直播**流。

造的流
    本机 HTTP 服务维护一个滚动窗口的 live playlist（3 个分片、每 6 秒前进一格、
    无 ENDLIST），分片内容是 Apple bipbop 测试流的真实 TS（PTS 连续）。
    `seg3.ts` 在**开播后的 20 秒内**恒定 404 —— 模拟「源站这一片暂时没了」。

    为什么是「持续 20 秒」而不是「只坏一次」：ExoPlayer 自带重试（指数退避
    1s/2s/4s），只坏一次的话第 2 次请求就成功了，根本走不到我们的自救逻辑。
    要坏足一整轮重试周期（>7s）才能把 fatal 逼出来，这时自救才有意义。

预期
    修复后：seg3 被重试到耗尽 → 原生自救（日志里有 "same-stream retry"）→
            seek 到直播边缘 → 此时坏片已不在窗口 / 已过期 → 分片 4/5 正常 →
            position 恢复推进，**不出现 error → 换线路**。
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
                   boot_app, cap, open_local, sh, toast_log)

PORT = 19557
CDN = ("https://devstreaming-cdn.apple.com/videos/streaming/examples/"
       "img_bipbop_adv_example_ts/v5/")
WIN = 3           # 窗口 3 个分片
TARGET = 6        # 每个分片 6 秒
BAD_SEQ = 3       # 恒坏的分片序号
BAD_SECONDS = 20  # 坏片持续多久（从第一次拉到 playlist 算起）
NPRE = 16         # 预下载多少分片

SEGS = {}
HITS = []          # (t, path, status)
T0 = [None]        # 第一次拉 playlist 的时刻
_CTX = ssl._create_unverified_context()


def adb(*a, timeout=40):
    return subprocess.run([ADB_PATH, "-s", SERIAL] + list(a),
                          capture_output=True, text=True, timeout=timeout)


def fetch_segments():
    banner("0) 预下载 %d 个真实 TS 分片" % NPRE)
    t0 = time.time()
    for i in range(NPRE):
        u = CDN + "fileSequence%d.ts" % i
        for a in range(3):
            try:
                SEGS[i] = urllib.request.urlopen(u, context=_CTX, timeout=30).read()
                break
            except Exception as e:
                if a == 2:
                    print("  分片 %d 失败 %s" % (i, e), flush=True)
                    return False
                time.sleep(1.5)
    print("  %d 个 / %.1f MB / %.1fs"
          % (len(SEGS), sum(len(v) for v in SEGS.values()) / 1048576.0, time.time() - t0),
          flush=True)
    return len(SEGS) >= NPRE


def base_now():
    """窗口起点：每 TARGET 秒前进一格。"""
    if T0[0] is None:
        return 0
    return int((time.time() - T0[0]) // TARGET)


def is_bad(seq):
    if T0[0] is None or seq != BAD_SEQ:
        return False
    return (time.time() - T0[0]) < BAD_SECONDS


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
        if p == "/live.m3u8":
            if T0[0] is None:
                T0[0] = time.time()
            base = base_now()
            L = ["#EXTM3U", "#EXT-X-VERSION:3",
                 "#EXT-X-TARGETDURATION:%d" % TARGET,
                 "#EXT-X-MEDIA-SEQUENCE:%d" % base]
            for i in range(base, base + WIN):
                L.append("#EXTINF:%d.00000," % TARGET)
                L.append("http://127.0.0.1:%d/seg%d.ts" % (PORT, i))
            b = ("\n".join(L) + "\n").encode()
            HITS.append((round(time.time() - (T0[0] or time.time()), 1), p, 200, base))
            self._send(200, b, "application/vnd.apple.mpegurl")
            return
        m = re.match(r"^/seg(\d+)\.ts$", p)
        if m and int(m.group(1)) in SEGS:
            i = int(m.group(1))
            if is_bad(i):
                HITS.append((round(time.time() - (T0[0] or time.time()), 1), p, 404, i))
                self._send(404)
                return
            HITS.append((round(time.time() - (T0[0] or time.time()), 1), p, 200, i))
            self._send(200, SEGS[i], "video/mp2t")
            return
        HITS.append((round(time.time() - (T0[0] or time.time()), 1), p, 404, -1))
        self._send(404)


def start_server():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


SAMPLE_JS = ("(function(){try{var s={};"
             "s.idx=(typeof cur!=='undefined')?cur.idx:-1;"
             "s.nat=NATIVE.active?1:0;s.ff=NATIVE.firstFrame?1:0;"
             "s.pos=-1;"
             "try{var j=JSON.parse(NativeTV.nativeStats());s.pos=j.pos;"
             "s.st=j.state;}catch(e){}"
             "s.stt=String(($('status')||{}).textContent||'').slice(0,26);"
             "return JSON.stringify(s);}catch(e){return '{\"e\":\"'+e+'\"}';}})()")


def main():
    if not fetch_segments():
        return 2
    srv = start_server()
    print("本机 live HLS 已就绪 :%d/live.m3u8（窗口 %d 片 · 每 %ds 前进一格 · "
          "seg%d 在开播 %ds 内恒 404）" % (PORT, WIN, TARGET, BAD_SEQ, BAD_SECONDS), flush=True)

    banner("1) 映射端口并启动 App")
    adb("reverse", "tcp:%d" % PORT, "tcp:%d" % PORT)
    cdp = boot_app()
    open_local(cdp)
    arm_toast_probe(cdp)

    banner("2) 注入 live 流并播放")
    url = "http://127.0.0.1:%d/live.m3u8" % PORT
    js = ("(function(){try{cur.tid='local';cur.idx=0;cur.urls=[%r];cur.epg=[];"
          "if(typeof showPlayerLayer==='function')showPlayerLayer();"
          "playIdx(0);return 1;}catch(e){return 'err:'+e;}})()" % url)
    print("inject:", cdp.eval_js(js, 10), flush=True)

    banner("3) 采样 55s")
    t0 = time.time()
    rows = []
    while time.time() - t0 < 55.0:
        try:
            s = json.loads(cdp.eval_js(SAMPLE_JS, 8) or "{}")
        except Exception as e:
            s = {"e": str(e)[:36]}
        s["t"] = round(time.time() - t0, 1)
        rows.append(s)
        print("[%5.1fs] idx=%s nat=%s ff=%s pos=%s st=%s | %s %s"
              % (s["t"], s.get("idx"), s.get("nat"), s.get("ff"), s.get("pos"),
                 s.get("st"), s.get("stt", ""), s.get("e", "")), flush=True)
        time.sleep(2.0)
    cap("hls_live.png")

    banner("4) 服务端请求时间线（关键证据）")
    for t, p, st, i in HITS:
        mark = ""
        if p == "/live.m3u8":
            mark = "  <- playlist base=%s" % i
        elif st == 404:
            mark = "  ★ 404"
        print("   [%6.1fs] %-12s %s%s" % (t, p, st, mark), flush=True)
    badn = len([1 for _, p, st, _ in HITS if st == 404 and p.startswith("/seg")])
    after_bad_ok = 0
    seen_bad = False
    for _, p, st, _ in HITS:
        if p.startswith("/seg") and st == 404:
            seen_bad = True
        elif seen_bad and p.startswith("/seg") and st == 200:
            after_bad_ok += 1

    banner("5) 结论")
    pos = [r.get("pos") for r in rows if isinstance(r.get("pos"), int)]
    pos = [p for p in pos if p >= 0]
    pmax, pmin = (max(pos), min(pos)) if pos else (-1, -1)
    print("  position 范围 %s ~ %s" % (pmin, pmax), flush=True)
    print("  坏片被拒 %d 次 · 坏片之后成功拿到 %d 个分片" % (badn, after_bad_ok), flush=True)
    print("  toast:", [t for _, t in toast_log(cdp)], flush=True)
    lg = sh(["logcat", "-d", "-v", "time"], timeout=40) or ""
    retry_lines = [l.strip() for l in lg.splitlines() if "same-stream retry" in l]
    print("  原生自救日志 %d 条：" % len(retry_lines), flush=True)
    for l in retry_lines[-4:]:
        print("     " + l[:120], flush=True)
    if after_bad_ok >= 1 and pmax > 0:
        print("  => ✅ 自救生效：坏片之后又拿到了正常分片，position 继续推进", flush=True)
    else:
        print("  => ❌ 仍然卡在坏片上（自救没救回来）", flush=True)

    adb("reverse", "--remove", "tcp:%d" % PORT)
    srv.shutdown()
    print("\nPKG=%s （探针结束）" % PKG, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

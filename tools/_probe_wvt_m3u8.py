# -*- coding: utf-8 -*-
"""
探测「官网源能不能提取出 m3u8、交给原生播放器播」—— 逐站点判定。

背景
----
官网源（WebViewTV）1114 条线路里只有 **61 条**本身就是直链（`.m3u8/.flv/.ts`），
其余 **1053 条**是官方直播页。本脚本回答：那些网页线路里，有多少能被
CDP 嗅探出真实流、并且**真能播**（而不是抓到地址却是加扰流）。

判据（三道，缺一不可）
---------------------
  1. 嗅探命中且评分 >= 100（真 m3u8，不是埋点/分片）
  2. 播放列表能 200 取回，且内容是 `#EXTM3U`
  3. mpv 跑满 300 帧，**解码错误事件数 / 300** 足够低

★ 第 3 条是关键：央视系（`cdrmldcctv1_1` 这种，`cdrm` = Content DRM）也能
  被嗅探出 m3u8，播放列表也正常，但**分片载荷是私有加扰的** ——
  码流里没有 `#EXT-X-KEY`（所以不是标准 HLS AES-128），可 h264 解码器
  几乎每帧都报错。表现为「能跑但不忍直视」，等价于播不了。

★ 请求头必须做逗号转义！`--http-header-fields` 是列表选项，UA 里的
  `KHTML, like Gecko` 不转义会让 mpv **rc=2 秒退**，被误判成「这流播不了」。
  本脚本的 `header_fields()` 复刻 `desktop/native_app.py` 的同名实现。

用法
----
    cd tv_all/desktop && python ../tools/_probe_wvt_m3u8.py
    python tools/_probe_wvt_m3u8.py "https://m.gdtv.cn/tvChannelDetail/43"
"""

import os
import re
import sys
import json
import time
import subprocess
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_DESKTOP = os.path.normpath(os.path.join(_HERE, "..", "desktop"))
sys.path.insert(0, _DESKTOP)

import cdp  # noqa: E402  （desktop/cdp.py）

MPV = os.path.join(_DESKTOP, "mpv", "mpv.exe")
KEEP = ("referer", "user-agent", "cookie", "origin")
FRAMES = 300
PROFILE = os.path.join(_DESKTOP, ".sniff_probe_m3u8")

# 三类代表站点：非 DRM 地方台 / 央视官网 / 央视频
DEFAULT_TARGETS = [
    ("非DRM·广东卫视", "https://m.gdtv.cn/tvChannelDetail/43"),
    ("央视·CCTV-1", "https://tv.cctv.com/live/cctv1/"),
    ("央视·央视频", "https://www.yangshipin.cn/tv/home?pid=600001859"),
]


def header_fields(headers):
    """复刻 native_app._header_fields —— 值里的逗号必须转义成 \\, """
    if not headers:
        return ""
    out = []
    for k in KEEP:
        for hk, hv in headers.items():
            if hk.lower() == k and hv:
                out.append("%s: %s" % (k, str(hv).replace(",", "\\,")))
    return ",".join(out)


def fetch_playlist(url, headers, n=6000):
    req = urllib.request.Request(url, headers={k: v for k, v in headers.items()})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, (r.headers.get("Content-Type") or ""), r.read(n).decode("utf-8", "replace")


def play_mpv(url, headers, frames=FRAMES, timeout=45):
    """跑满 frames 帧，返回 (rc, 秒数, 解码错误事件数, VO 行)。"""
    hf = header_fields(headers)
    args = [MPV, url, "--no-config", "--vo=null", "--ao=null",
            "--frames=%d" % frames, "--cache=no", "--hwdec=no", "--ytdl=no",
            "--msg-level=all=info"]
    if hf:
        args.append("--http-header-fields=" + hf)
    t = time.time()
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, time.time() - t, -1, ""
    out = (p.stdout + p.stderr).decode("utf-8", "replace")
    errs = len(re.findall(
        r"error while decoding|Reference \d+ >=|top block unavailable|corrupt", out))
    vo = ""
    for l in out.splitlines():
        if l.startswith("VO:"):
            vo = l.strip()
            break
    return p.returncode, time.time() - t, errs, vo


def probe(name, page_url):
    print("=" * 74)
    print("### %s\n    %s" % (name, page_url))
    r = cdp.sniff_stream(page_url, timeout=26, offscreen=True,
                         profile_dir=PROFILE, log=lambda *a: None)
    if not r:
        print("    → 嗅探未命中")
        return {"name": name, "page": page_url, "verdict": "未命中"}
    u, h = r["url"], r.get("headers") or {}
    print("    流地址: %s" % u[:120])

    try:
        st, ct, body = fetch_playlist(u, h)
    except Exception as e:
        print("    → 播放列表取回失败: %r" % (e,))
        return {"name": name, "page": page_url, "stream": u, "verdict": "列表取不到"}
    is_m3u8 = body.lstrip().startswith("#EXTM3U")
    print("    HTTP=%s  ctype=%s  是 m3u8: %s" % (st, ct, is_m3u8))

    rc, dt, errs, vo = play_mpv(u, h)
    if rc is None:
        print("    → mpv 超时，没跑满 %d 帧" % FRAMES)
        return {"name": name, "page": page_url, "stream": u, "verdict": "超时"}
    rate = (errs / float(FRAMES)) if errs >= 0 else -1
    print("    mpv rc=%s  %.1fs  解码错误=%d (%.2f 次/帧)  %s" % (rc, dt, errs, rate, vo))
    verdict = "可播 ✔" if (rc == 0 and rate < 0.1) else ("能跑但花屏 ✘" if rc == 0 else "播不了 ✘")
    print("    → %s" % verdict)
    return {"name": name, "page": page_url, "stream": u, "ctype": ct,
            "m3u8": is_m3u8, "errs": errs, "err_rate": round(rate, 3), "verdict": verdict}


def main():
    argv = sys.argv[1:]
    targets = [("自定义", argv[0])] if argv else DEFAULT_TARGETS
    print("官网源 m3u8 提取探测 · 每站点跑满 %d 帧" % FRAMES)
    print("提示：每个站点需要跑一次真浏览器，约 30~40 秒\n")
    out = [probe(n, u) for n, u in targets]
    print("\n" + "=" * 74)
    print("汇总")
    for o in out:
        print("  %-16s %s" % (o["name"], o.get("verdict")))
    print("\nJSON:")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

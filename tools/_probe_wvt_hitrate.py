#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
官网源「提取 m3u8 交原生播放」命中率实测。

回答的问题：官网源里那 94.5% 的网页线路，到底有多少能真的提取出流、并且是干净可播的？
（不是「有没有 m3u8」，而是「提取出来 mpv 能不能解出画面」。）

方法（每个站点）：
  1. cdp.sniff_stream 用真浏览器加载官方直播页，抓媒体请求里的 m3u8
  2. 解析播放列表：若是 master（含 EXT-X-STREAM-INF）则**下钻到子播放列表**再取分片
  3. 分片判型：TS（首字节 0x47）/ fMP4（偏移 4 处 ftyp|styp|moof）/ 其他
  4. mpv 无窗口跑 N 帧 → 数解码错误 + 看是否真建起视频输出（VO 行）

★ 判据必须是「mpv 真解出画面 + 解码错误率」，**不是 rc == 0**：
  - 央视系私有加扰流 mpv 也能跑完、rc=0，但几乎每帧报 error while decoding；
  - 反过来，rc!=0 也可能只是探针自身的问题。
★ 分片判型的三个坑（都踩过）：
  - master playlist 的第一个非 # 行是**子播放列表**，不是分片；
  - HLS 早已普遍用 **fMP4** 分片，首字节不是 0x47 —— 只认 0x47 会漏判；
  - 从「取到的字节是 #EXTM3U」反推分片地址无效，必须递归解析。
★ --http-header-fields 的值里逗号必须转义成 \\, ——
  UA 里的 "KHTML, like Gecko" 会把 mpv 的列表选项解析搞崩（项目老坑）。

用法：python tools/_probe_wvt_hitrate.py
"""
import os, re, sys, json, time, subprocess, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "desktop"))
import cdp  # noqa: E402

MPV = os.path.join(ROOT, "desktop", "mpv", "mpv.exe")
KEEP = ("referer", "user-agent", "cookie", "origin")
MEDIA = (".m3u8", ".flv", ".ts", ".mp4", ".mkv", ".mpd")
CCTV_KW = ("cctv.com", "cctv.cn", "cntv.cn", "yangshipin", "ysp.cctv")

FRAMES = 150          # 跑 150 帧判「能不能稳定解出画面」
SNIFF_TIMEOUT = 22
ERR_LIMIT = 12        # 150 帧里超过 12 次解码错误就算「花屏不可用」

SAMPLES = ["m.gdtv.cn", "www.cditv.cn", "www.nmtv.cn", "static.hntv.tv",
           "www.sxrtv.com", "www.jlntv.cn", "live.jstv.com", "www.sctv.com",
           "www.cztv.com", "live.kankanews.com", "www.btime.com", "tv.gxtv.cn",
           "iptv345.com", "www.mgtv.com", "web.guangdianyun.tv"]


def esc(headers):
    """复刻 native_app._header_fields：值里的逗号转义成 \\, """
    if not headers:
        return ""
    out = []
    for k in KEEP:
        for hk, hv in headers.items():
            if hk.lower() == k and hv:
                out.append("%s: %s" % (k, str(hv).replace(",", "\\,")))
    return ",".join(out)


def http_get(url, headers, n=20000, timeout=14):
    req = urllib.request.Request(url, headers={k: v for k, v in (headers or {}).items()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(n)


def abs_url(u, base_url):
    if u.startswith("http"):
        return u
    if u.startswith("/"):
        sp = urllib.request.urlsplit(base_url)
        return "%s://%s%s" % (sp.scheme, sp.netloc, u)
    return base_url.rsplit("/", 1)[0] + "/" + u


def resolve(url, headers, depth=0):
    """
    解析播放列表，必要时下钻 master → 子列表。
    返回 (playlist_text, media_url, [media_url...], key_line, seg_url, seg_head_bytes)
    """
    text = http_get(url, headers).decode("utf-8", "replace")
    key = next((l for l in text.splitlines() if "EXT-X-KEY" in l), "")
    levels = []
    cur, cur_text, hops = url, text, 0
    while hops < 3:
        nxt = None
        for line in cur_text.splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                nxt = abs_url(s, cur)
                break
        if not nxt:
            break
        levels.append(nxt)
        # 子项还是 m3u8 → 继续下钻
        if nxt.lower().split("?")[0].endswith(".m3u8") or ".m3u8" in nxt.lower():
            try:
                cur_text = http_get(nxt, headers).decode("utf-8", "replace")
                cur = nxt
                hops += 1
                continue
            except Exception:
                break
        # 真正的分片
        head = http_get(nxt, headers, n=32)
        return cur_text, cur, levels, key, nxt, head
    return cur_text, cur, levels, key, None, None


def seg_kind(head):
    if not head:
        return "?"
    if head[:1] == b"\x47":
        return "TS"
    if head[4:8] in (b"ftyp", b"styp", b"moof", b"moov", b"sidx"):
        return "fMP4"
    if head[:7] == b"#EXTM3U":
        return "仍是列表"
    return "其他"


def probe_mpv(url, headers):
    """mpv 无窗口跑 FRAMES 帧，返回 (完成, rc, 秒, 解码错误数, 是否建起视频输出, 末尾日志)。"""
    hf = esc(headers)
    args = [MPV, url, "--no-config", "--vo=null", "--ao=null",
            "--frames=%d" % FRAMES, "--cache=no", "--hwdec=no", "--ytdl=no",
            "--msg-level=all=info"] + (["--http-header-fields=" + hf] if hf else [])
    t = time.time()
    try:
        p = subprocess.run(args, capture_output=True, timeout=45)
    except subprocess.TimeoutExpired:
        return False, -999, time.time() - t, 0, False, ["<超时：没能跑完 %d 帧>" % FRAMES]
    out = (p.stdout + p.stderr).decode("utf-8", "replace")
    errs = len(re.findall(
        r"error while decoding|Reference \d+ >=|top block unavailable|corrupt|"
        r"Invalid NAL|decode_slice_header", out))
    # VO: [null] 1920x1080 —— 出现它说明真的解出了视频帧
    got_video = bool(re.search(r"VO:\s*\[\w+\]\s*\d+x\d+", out))
    tail = [l for l in out.splitlines() if l.strip()][-4:]
    return p.returncode == 0, p.returncode, time.time() - t, errs, got_video, tail


def verdict_of(done, errs, got_video):
    if errs > ERR_LIMIT:
        return "花屏(错误%d)✘" % errs
    if not done or not got_video:
        return "失败✘"
    return "可播 ✔"


def load_samples():
    """从快照里每个非央视域名挑一个页面样本。"""
    d = json.load(open(os.path.join(ROOT, "shared", "wvt_builtin.json"),
                       encoding="utf-8"))
    seen = {}
    for c in d.get("channels") or []:
        for u in (c.get("urls") or []):
            url = u.get("url", "")
            if not url or url.lower().split("?")[0].endswith(MEDIA):
                continue
            h = re.sub(r"^https?://", "", url).split("/")[0].lower()
            if any(k in h for k in CCTV_KW):
                continue
            if h not in seen:
                seen[h] = (c.get("name", ""), url)
    return seen


def main():
    seen = load_samples()
    results = []

    def run(h):
        if h not in seen:
            print("[--] %-24s 快照里没有" % h, flush=True)
            return
        cname, page = seen[h]
        print("=" * 76, flush=True)
        print("[..] %-24s %s | %s" % (h, cname, page), flush=True)
        t0 = time.time()
        try:
            hit = cdp.sniff_stream(page, timeout=SNIFF_TIMEOUT, offscreen=True,
                                   profile_dir=".sniff_hitrate", log=lambda *a: None)
        except Exception as e:
            print("     嗅探异常: %r" % (e,), flush=True)
            hit = None
        if not hit:
            print("     >>> 未嗅到流（%.1fs）  ✘" % (time.time() - t0), flush=True)
            results.append((h, cname, "未命中", "", 0, time.time() - t0))
            return

        u, hd = hit["url"], hit.get("headers") or {}
        print("     流: %s" % u[:118], flush=True)
        enc, kind, note = "?", "?", ""
        try:
            _, media_url, levels, keyl, seg, head = resolve(u, hd)
            enc = "AES-128" if (keyl and "METHOD=NONE" not in keyl) else "无KEY"
            kind = seg_kind(head)
            print("     下钻 %d 层 → %s" % (len(levels), media_url[:100]), flush=True)
            print("     加密: %-8s 分片: %s (%s)"
                  % (enc, kind, (head or b"")[:12].hex()), flush=True)
        except Exception as e:
            note = "解析失败:%s" % repr(e)[:50]
            print("     播放列表解析异常: %s" % note, flush=True)

        ok, rc, dt, errs, got_video, tail = probe_mpv(u, hd)
        v = verdict_of(ok, errs, got_video)
        print("     mpv %d 帧: rc=%s 时长=%.1fs 解码错误=%d 视频输出=%s  → %s"
              % (FRAMES, rc, dt, errs, "有" if got_video else "无", v), flush=True)
        for l in tail:
            print("        | %s" % l[:130], flush=True)
        results.append((h, cname, v, enc, errs, time.time() - t0))

    for h in SAMPLES:
        run(h)

    print("\n" + "=" * 76, flush=True)
    print("汇总（%d 个抽样的地方台/聚合站官网）" % len(results), flush=True)
    print("%-24s %-14s %-18s %-8s %s" % ("域名", "频道", "结果", "加密", "耗时"), flush=True)
    n_hit = n_ok = 0
    for h, cname, v, enc, errs, dt in results:
        if v != "未命中":
            n_hit += 1
        if v == "可播 ✔":
            n_ok += 1
        print("%-24s %-14s %-18s %-8s %.0fs" % (h, cname, v, enc, dt), flush=True)
    print("\n嗅到流: %d/%d   其中干净可播: %d" % (n_hit, len(results), n_ok), flush=True)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()

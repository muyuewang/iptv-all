# -*- coding: utf-8 -*-
"""从 m3u 源文件抽取「频道名 -> 台标URL」映射，输出 app/assets/logos.js

用法： python gen_logos.py [源目录]
默认源目录：../../../iptv/src2（旧工程的台标素材），也可传参覆盖。
"""
import re, os, glob, json

_HERE = os.path.dirname(os.path.abspath(__file__))
_ASSETS = os.path.join(_HERE, "..", "app", "assets")

# 默认去旧工程找素材；找不到就提示（logos.js 已随工程提供，通常无需重跑）
SRC = _HERE
for _c in (os.path.join(_HERE, "..", "..", "..", "iptv", "src2"),
           os.path.join(_HERE, "..", "..", "iptv", "src2"),
           os.path.join(_HERE, "src2")):
    if os.path.isdir(_c):
        SRC = _c
        break
OUT = os.path.join(_ASSETS, "logos.js")

def norm(n):
    n = re.sub(r'[\s　]+', '', n)
    return n

logo = {}
for fp in glob.glob(os.path.join(SRC, "*")):
    try:
        txt = open(fp, encoding="utf-8", errors="ignore").read()
    except Exception:
        continue
    for line in txt.splitlines():
        if not line.startswith("#EXTINF"):
            continue
        ml = re.search(r'tvg-logo="([^"]+)"', line)
        if not ml:
            continue
        url = ml.group(1)
        if url.startswith("http") is False:
            continue
        mn = re.search(r'tvg-name="([^"]+)"', line)
        if mn:
            name = mn.group(1)
        else:
            parts = [p for p in line.split(",") if p and "tvg-" not in p and "group-title" not in p]
            if not parts:
                continue
            name = parts[-1]
        name = re.sub(r'\s*\(?\d{3,4}p\)?\s*$', '', name).strip()
        name = re.sub(r'\s*(HEVC|50 ?FPS|4K|HD|超清|高清)\s*$', '', name, flags=re.I).strip()
        key = norm(name)
        if key and key not in logo:
            logo[key] = url

# 常见 CCTV 台标兜底（fanmingming）
for i in list(range(1, 18)) + ["5+"]:
    logo.setdefault("CCTV" + str(i), f"https://live.fanmingming.cn/tv/CCTV-{i}.png")

js = "window.LOGOS=" + json.dumps(logo, ensure_ascii=False) + ";"
open(os.path.join(os.path.dirname(OUT), "logos.js"), "w", encoding="utf-8").write(js)
print("台标条目:", len(logo))
print("示例:", list(logo.items())[:3])

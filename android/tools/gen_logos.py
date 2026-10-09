# -*- coding: utf-8 -*-
"""从已有源文件抽取 频道名 -> 台标URL 映射，输出 app/assets/logos.json"""
import re, os, glob, json

SRC = r"C:\Users\fengxin\WorkBuddy\2026-10-09-14-02-22\iptv\src2"
OUT = r"C:\Users\fengxin\WorkBuddy\2026-10-09-14-02-22\apk_clean\app\assets\logos.json"

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

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""_probe_orient_flash.py —— 把 _shots_orient/s*.png 逐帧统计 + 拼成联络表。

用法：
    python tools/_probe_orient_flash.py [帧目录] [输出拼图]

判据说明：Chromium 默认媒体控件（大播放键 + 0:00 轨道）出现时，画面里会有一块
「中灰渐变区 + 大块暗色圆盘」。这里用两个可量化信号近似：
    gray%  —— 亮度落在 [45,205] 的像素占比（渐变底 + 控件底）
    disc%  —— 中心区域（画面中央 60%×70%）亮度 < 40 的占比（播放键圆盘/三角）
正常播放时 video 全屏、内容千变万化；出现默认控件时 gray% 与 disc% 会同时显著。
"""
import sys, os, glob
from PIL import Image, ImageStat, ImageDraw

SRC = sys.argv[1] if len(sys.argv) > 1 else "_shots_orient"
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(SRC, "sheet.png")

files = sorted(glob.glob(os.path.join(SRC, "s*.png")))
if not files:
    print("没有帧"); sys.exit(1)

rows = []
for f in files:
    im = Image.open(f).convert("L")
    W, H = im.size
    px = im.load()
    step = 4
    tot = mid = dark = 0
    cx0, cx1 = int(W*0.20), int(W*0.80)
    cy0, cy1 = int(H*0.15), int(H*0.85)
    for y in range(0, H, step):
        for x in range(0, W, step):
            v = px[x, y]
            tot += 1
            if 45 <= v <= 205:
                mid += 1
            if cx0 <= x < cx1 and cy0 <= y < cy1 and v < 40:
                dark += 1
    gray = 100.0*mid/tot
    disc = 100.0*dark/(((cx1-cx0)//step)*((cy1-cy0)//step))
    st = ImageStat.Stat(im)
    rows.append((os.path.basename(f), W, H, st.mean[0], st.stddev[0], gray, disc))

print(f"{'帧':8s} {'尺寸':10s} {'均值':>6s} {'σ':>6s} {'gray%':>7s} {'disc%':>7s}")
for n, W, H, m, s, g, d in rows:
    flag = "  ← 疑似默认控件" if (g > 25 and d > 12) else ""
    print(f"{n:8s} {W}x{H:<6d} {m:6.1f} {s:6.1f} {g:7.1f} {d:7.1f}{flag}")

# 拼图
TH = 200
tiles = []
for f in files:
    im = Image.open(f).convert("RGB")
    w = max(1, int(im.width * TH / im.height))
    tiles.append((os.path.basename(f), im.resize((w, TH), Image.LANCZOS)))

COLS = min(6, len(tiles))
GAP, LBL = 6, 16
colw = [max(t[1].width for t in tiles[i::COLS]) for i in range(COLS)]
rowh = [max(t[1].height for t in tiles[r*COLS:(r+1)*COLS]) for r in range((len(tiles)+COLS-1)//COLS)]
Wt = sum(colw) + GAP*(COLS+1)
Ht = sum(h+LBL for h in rowh) + GAP*(len(rowh)+1)
sheet = Image.new("RGB", (Wt, Ht), (24, 26, 32))
d = ImageDraw.Draw(sheet)
y = GAP
for r in range(len(rowh)):
    x = GAP
    for c in range(COLS):
        i = r*COLS + c
        if i >= len(tiles):
            break
        name, t = tiles[i]
        sheet.paste(t, (x, y+LBL))
        d.text((x+2, y+3), name, fill=(220, 220, 220))
        x += colw[c] + GAP
    y += rowh[r] + LBL + GAP
sheet.save(OUT)
print("拼图 →", OUT, sheet.size)

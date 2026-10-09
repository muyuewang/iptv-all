# -*- coding: utf-8 -*-
"""生成 Android TV Banner(320x180) 与 App 图标(192x192)"""
import os
from PIL import Image, ImageDraw, ImageFont

ROOT = r"C:\Users\fengxin\WorkBuddy\2026-10-09-14-02-22\apk_clean\app"
FONT = r"C:\Windows\Fonts\msyhbd.ttc"
BG = (15, 17, 21)
ACCENT = (229, 57, 53)
FG = (232, 234, 237)

def font(sz):
    try:
        return ImageFont.truetype(FONT, sz)
    except Exception:
        return ImageFont.load_default()

def banner(path):
    W, H = 320, 180
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    # 渐变条
    for x in range(W):
        c = int(10 + 20 * x / W)
        d.line([(x, 0), (x, H)], fill=(c, c + 2, c + 6))
    # play 三角
    cx, cy, s = 78, 90, 34
    d.polygon([(cx - s // 2, cy - s), (cx - s // 2, cy + s), (cx + s, cy)], fill=ACCENT)
    # 文本
    d.text((132, 52), "纯净电视", font=font(40), fill=FG)
    d.text((134, 104), "TV  ·  直播", font=font(22), fill=(154, 160, 166))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im.save(path)

def icon(path):
    S = 192
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, S, S], radius=42, fill=BG)
    cx, cy, s = 96, 92, 40
    d.polygon([(cx - s // 2, cy - s), (cx - s // 2, cy + s), (cx + s, cy)], fill=ACCENT)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im.save(path)

banner(os.path.join(ROOT, "res", "drawable-xhdpi", "tv_banner.png"))
icon(os.path.join(ROOT, "res", "mipmap-xxhdpi", "ic_launcher.png"))
print("生成完成:",
      os.path.join(ROOT, "res", "drawable-xhdpi", "tv_banner.png"),
      os.path.join(ROOT, "res", "mipmap-xxhdpi", "ic_launcher.png"))

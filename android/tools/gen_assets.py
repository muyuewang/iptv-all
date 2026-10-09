# -*- coding: utf-8 -*-
"""生成 Android TV Banner(320x180) 与 App 图标(192x192)

用法： python gen_assets.py
输出： ../app/res/drawable-xhdpi/tv_banner.png、../app/res/mipmap-xxhdpi/ic_launcher.png
"""
import os
from PIL import Image, ImageDraw, ImageFont

# 相对本脚本定位，避免工程挪动后写错位置
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
FONT_CANDS = [
    r"C:\Windows\Fonts\msyhbd.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]
BG = (10, 11, 15)
ACCENT = (255, 45, 85)      # 与 UI 品牌色一致
FG = (242, 244, 248)

def font(sz):
    for f in FONT_CANDS:
        if os.path.exists(f):
            try:
                return ImageFont.truetype(f, sz)
            except Exception:
                continue
    return ImageFont.load_default()

def _grad(w, h, c1, c2, diag=True):
    """红→暖橙渐变底图"""
    im = Image.new("RGB", (w, h), c1)
    px = im.load()
    for y in range(h):
        for x in range(w):
            t = (x / max(w - 1, 1) + (y / max(h - 1, 1) if diag else 0)) / (2 if diag else 1)
            px[x, y] = (int(c1[0] + (c2[0] - c1[0]) * t),
                        int(c1[1] + (c2[1] - c1[1]) * t),
                        int(c1[2] + (c2[2] - c1[2]) * t))
    return im

def banner(path):
    W, H = 320, 180
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    # 深色冷调渐变背景
    for x in range(W):
        for y in range(H):
            t = y / H
            c = int(14 + 14 * (1 - t))
            im.putpixel((x, y), (c + 4, c + 2, c + 8))
    d = ImageDraw.Draw(im)
    # 品牌渐变播放三角
    cx, cy, s = 76, 90, 32
    tri = [(cx - s // 2, cy - s), (cx - s // 2, cy + s), (cx + s, cy)]
    # 三角用渐变填充（逐行裁剪）
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).polygon(tri, fill=255)
    im.paste(_grad(W, H, ACCENT, (255, 107, 53)), (0, 0), mask)
    d = ImageDraw.Draw(im)
    d.text((130, 50), "纯净电视", font=font(40), fill=FG)
    d.text((133, 102), "TV  ·  LIVE", font=font(21), fill=(150, 157, 172))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im.save(path)

def icon(path):
    S = 192
    # 圆角渐变底
    base = _grad(S, S, ACCENT, (255, 107, 53)).convert("RGBA")
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S, S], radius=44, fill=255)
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    im.paste(base, (0, 0), mask)
    d = ImageDraw.Draw(im)
    # 白色播放三角
    cx, cy, s = 100, 96, 34
    d.polygon([(cx - s // 2, cy - s), (cx - s // 2, cy + s), (cx + s, cy)], fill=(255, 255, 255))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im.save(path)

banner(os.path.join(ROOT, "res", "drawable-xhdpi", "tv_banner.png"))
icon(os.path.join(ROOT, "res", "mipmap-xxhdpi", "ic_launcher.png"))
print("生成完成:",
      os.path.join(ROOT, "res", "drawable-xhdpi", "tv_banner.png"),
      os.path.join(ROOT, "res", "mipmap-xxhdpi", "ic_launcher.png"))

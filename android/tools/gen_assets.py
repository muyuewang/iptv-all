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
    """App 图标：电视/显示器造型 + 品牌红橙渐变（与桌面端 app.ico 统一）"""
    S = 192
    SS = S * 4  # 超采样
    im = Image.new("RGBA", (SS, SS), (0, 0, 0, 0))
    g = _grad(SS, SS, ACCENT, (255, 107, 53)).convert("RGBA")

    # 机身（屏幕外框）
    body_x = SS * 0.075
    body_y = SS * 0.115
    body_w = SS - body_x * 2
    body_h = body_w * 0.72
    body_r = body_w * 0.11
    mask = Image.new("L", (SS, SS), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [body_x, body_y, body_x + body_w, body_y + body_h], radius=body_r, fill=255)
    im.paste(g, (0, 0), mask)

    # 底座（与机身相连）
    stand_w = body_w * 0.36
    stand_h = SS * 0.062
    stand_x = (SS - stand_w) / 2
    stand_y = body_y + body_h - SS * 0.02
    neck_w = body_w * 0.16
    neck_x = (SS - neck_w) / 2
    gm = Image.new("L", (SS, SS), 0)
    dm = ImageDraw.Draw(gm)
    dm.rounded_rectangle([neck_x, body_y + body_h - SS * 0.045, neck_x + neck_w, stand_y + stand_h * 0.5],
                         radius=neck_w * 0.3, fill=255)
    dm.rounded_rectangle([stand_x, stand_y, stand_x + stand_w, stand_y + stand_h],
                         radius=stand_h * 0.45, fill=255)
    im.paste(g, (0, 0), gm)

    # 屏幕暗底
    sc_pad = body_w * 0.075
    sc_x, sc_y = body_x + sc_pad, body_y + sc_pad
    sc_w, sc_h = body_w - sc_pad * 2, body_h - sc_pad * 2
    sc_r = body_r * 0.62
    sm = Image.new("L", (SS, SS), 0)
    ImageDraw.Draw(sm).rounded_rectangle([sc_x, sc_y, sc_x + sc_w, sc_y + sc_h], radius=sc_r, fill=255)
    im.paste(Image.new("RGB", (SS, SS), (16, 16, 22)).convert("RGBA"), (0, 0), sm)

    # 屏幕内白色播放三角
    d = ImageDraw.Draw(im)
    cx, cy = sc_x + sc_w * 0.54, sc_y + sc_h * 0.5
    tw, th = sc_w * 0.62, sc_h * 0.70
    d.polygon([(cx - tw / 2, cy - th / 2), (cx - tw / 2, cy + th / 2), (cx + tw / 2, cy)],
              fill=(255, 255, 255, 255))
    # 机身高光内描边
    d.rounded_rectangle([body_x + 1, body_y + 1, body_x + body_w - 1, body_y + body_h - 1],
                        radius=body_r, outline=(255, 255, 255, 55), width=max(2, SS // 110))

    out = im.resize((S, S), Image.LANCZOS)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    out.save(path)

banner(os.path.join(ROOT, "res", "drawable-xhdpi", "tv_banner.png"))
icon(os.path.join(ROOT, "res", "mipmap-xxhdpi", "ic_launcher.png"))
print("生成完成:",
      os.path.join(ROOT, "res", "drawable-xhdpi", "tv_banner.png"),
      os.path.join(ROOT, "res", "mipmap-xxhdpi", "ic_launcher.png"))

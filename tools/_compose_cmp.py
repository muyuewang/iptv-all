# -*- coding: utf-8 -*-
"""拼一张「源条 vs 分类胶囊」样式对照图（交付用）。"""
import os
from PIL import Image, ImageDraw, ImageFont

FONT = "C:/Windows/Fonts/msyh.ttc"
try:
    F = ImageFont.truetype(FONT, 17)
except Exception:
    F = ImageFont.load_default()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SH = os.path.join(ROOT, "_shots")

d_local = Image.open(os.path.join(SH, "ui_desktop_local.png")).convert("RGB")
d_wvt = Image.open(os.path.join(SH, "ui_desktop_wvt.png")).convert("RGB")
a_loc = Image.open(os.path.join(SH, "android_srcbar_local.png")).convert("RGB")

# 桌面：源条在 y≈28..72，分类胶囊在 y≈86..250（x 30..470）
crop_d1 = d_local.crop((28, 22, 470, 254))
crop_d2 = d_local.crop((28, 22, 470, 76))
crop_d3 = d_wvt.crop((28, 22, 470, 76))
# 安卓：源条 y≈84..152，分类 y≈228..338（x 10..600）
crop_a1 = a_loc.crop((10, 74, 600, 160))
crop_a2 = a_loc.crop((10, 226, 600, 338))

SCALE = 2
PAD = 18
LABEL_H = 30


def up(im, s=SCALE):
    return im.resize((im.width * s, im.height * s), Image.LANCZOS)


crops = [
    ("桌面端 · 本机源：源条 + 分类胶囊", up(crop_d1)),
    ("桌面端 · 本机源 源条（与分类胶囊同一 PillBar 控件）", up(crop_d2)),
    ("桌面端 · 官网源 源条（切换后同一控件、同一外观）", up(crop_d3)),
    ("安卓端 · 源条 + 分类胶囊（同一套 CSS：圆角/内距/渐变）", up(crop_a1)),
    ("安卓端 · 分类胶囊", up(crop_a2)),
]

W = max(c.width for _, c in crops) + PAD * 2
H = sum(c.height + LABEL_H + PAD for _, c in crops) + PAD
canvas = Image.new("RGB", (W, H), (14, 16, 20))
dr = ImageDraw.Draw(canvas)
y = PAD
for label, im in crops:
    dr.text((PAD, y + 6), label, fill=(160, 168, 182), font=F)
    y += LABEL_H
    canvas.paste(im, (PAD, y))
    dr.rectangle([PAD - 1, y - 1, PAD + im.width, y + im.height],
                 outline=(38, 42, 50))
    y += im.height + PAD

# 桌面端源条用的「同款」说明行
out = os.path.join(SH, "cmp_srcbar_style.png")
canvas.save(out)
print(out, canvas.size)

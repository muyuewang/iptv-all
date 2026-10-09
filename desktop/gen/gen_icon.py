# -*- coding: utf-8 -*-
"""生成桌面版图标 app.ico —— 电视/显示器造型 + 品牌红橙渐变

设计：
- 主体：一台电视（圆角屏幕边框 + 底座支架），屏幕内是白色播放三角
- 配色：屏幕边框与底座用品牌渐变 #ff2d55 → #ff6b35，屏幕内为深色
- 小尺寸（16/24/32）单独手绘：省略底座细节，三角更大，保证任务栏可辨
- 大尺寸（48+）：完整电视造型，含高光与柔和投影

用法： python gen_icon.py
输出： ../app.ico
"""
import os
from PIL import Image, ImageDraw, ImageFilter

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app.ico")

# 品牌渐变（与 UI --brand-grad 一致）
C1 = (255, 45, 85)      # #ff2d55 红
C2 = (255, 107, 53)     # #ff6b35 橙
SCREEN = (16, 16, 22)   # 屏幕底色（近黑，与 UI --bg 同系）
SCREEN_2 = (28, 20, 30)  # 屏幕底部微光


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def gradient(size, diag=True):
    im = Image.new("RGB", (size, size), C1)
    px = im.load()
    for y in range(size):
        for x in range(size):
            t = ((x + y) / max(2 * (size - 1), 1)) if diag else (y / max(size - 1, 1))
            px[x, y] = _lerp(C1, C2, t)
    return im


def screen_fill(size):
    """屏幕内的暗色，带一点从下往上的微光，避免死黑。"""
    im = Image.new("RGB", (size, size), SCREEN)
    px = im.load()
    for y in range(size):
        t = 1 - (y / max(size - 1, 1))          # 底部 t 大
        c = _lerp(SCREEN, SCREEN_2, t * 0.75)
        for x in range(size):
            px[x, y] = c
    return im


def _tri(d, x0, y0, w, h, color=(255, 255, 255, 255)):
    """在 (x0,y0) 左上角、宽 w 高 h 的范围内画播放三角（视觉居中）。"""
    cx, cy = x0 + w / 2, y0 + h / 2
    tw = w * 0.62
    th = h * 0.70
    ox = w * 0.04
    d.polygon([(cx - tw / 2 + ox, cy - th / 2),
               (cx - tw / 2 + ox, cy + th / 2),
               (cx + tw / 2 + ox, cy)], fill=color)


def draw_big(size):
    """大尺寸：完整电视造型（屏幕 + 边框 + 底座）。"""
    ss = size * 4
    img = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))

    # 电视整体：屏幕上沿到机身底
    margin = ss * 0.075
    body_w = ss - margin * 2
    body_h = body_w * 0.72          # 机身宽高比（含边框）
    body_x = margin
    body_y = ss * 0.115
    body_r = body_w * 0.11          # 机身圆角

    # 底座（在机身下方，与机身轻微重叠，避免视觉断裂）
    stand_w = body_w * 0.36
    stand_h = ss * 0.062
    stand_x = (ss - stand_w) / 2
    stand_y = body_y + body_h - ss * 0.02
    neck_w = body_w * 0.16
    neck_h = ss * 0.05
    neck_x = (ss - neck_w) / 2

    # 阴影
    sh = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    ds = ImageDraw.Draw(sh)
    ds.rounded_rectangle([body_x, body_y + ss * 0.02, body_x + body_w, body_y + body_h + ss * 0.02],
                         radius=body_r, fill=(120, 0, 20, 150))
    ds.rounded_rectangle([stand_x, stand_y + ss * 0.02, stand_x + stand_w, stand_y + stand_h + ss * 0.02],
                         radius=stand_h * 0.45, fill=(120, 0, 20, 130))
    sh = sh.filter(ImageFilter.GaussianBlur(ss * 0.022))
    img.alpha_composite(sh)

    # 机身用品牌渐变
    g = gradient(ss).convert("RGBA")
    mask = Image.new("L", (ss, ss), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [body_x, body_y, body_x + body_w, body_y + body_h], radius=body_r, fill=255)
    img.paste(g, (0, 0), mask)

    # 底座（渐变，稍暗）
    gm = Image.new("L", (ss, ss), 0)
    dm = ImageDraw.Draw(gm)
    # 支颈：从机身底向上接入，保证与机身连成一体
    neck_top = body_y + body_h - ss * 0.045
    dm.rounded_rectangle([neck_x, neck_top, neck_x + neck_w, stand_y + stand_h * 0.5],
                         radius=neck_w * 0.3, fill=255)
    dm.rounded_rectangle([stand_x, stand_y, stand_x + stand_w, stand_y + stand_h],
                         radius=stand_h * 0.45, fill=255)
    img.paste(g, (0, 0), gm)

    # 屏幕（内嵌暗色）
    sc_pad = body_w * 0.075
    sc_x, sc_y = body_x + sc_pad, body_y + sc_pad
    sc_w, sc_h = body_w - sc_pad * 2, body_h - sc_pad * 2
    sc_r = body_r * 0.62
    sfill = screen_fill(ss).convert("RGBA")
    sm = Image.new("L", (ss, ss), 0)
    ImageDraw.Draw(sm).rounded_rectangle([sc_x, sc_y, sc_x + sc_w, sc_y + sc_h], radius=sc_r, fill=255)
    img.paste(sfill, (0, 0), sm)

    # 屏幕内播放三角（渐变白）
    dd = ImageDraw.Draw(img)
    _tri(dd, sc_x, sc_y, sc_w, sc_h)

    # 机身高光内描边
    dd.rounded_rectangle([body_x + 1, body_y + 1, body_x + body_w - 1, body_y + body_h - 1],
                         radius=body_r, outline=(255, 255, 255, 55), width=max(2, ss // 110))
    return img.resize((size, size), Image.LANCZOS)


def draw_small(size):
    """小尺寸：简化为「屏幕块 + 加粗三角 + 一小截底座」，优先任务栏辨识度。"""
    ss = size * 8
    img = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))

    # 屏幕块：小尺寸放大占比，底座紧贴机身
    body_x = ss * 0.045
    body_y = ss * 0.075
    body_w = ss - body_x * 2
    body_h = body_w * 0.80
    body_r = body_w * 0.15

    g = gradient(ss).convert("RGBA")
    mask = Image.new("L", (ss, ss), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [body_x, body_y, body_x + body_w, body_y + body_h], radius=body_r, fill=255)
    img.paste(g, (0, 0), mask)

    # 底座：更宽更矮、向上与机身重叠，小图下连成一体
    stand_w = body_w * 0.50
    stand_h = ss * 0.095
    stand_x = (ss - stand_w) / 2
    stand_y = body_y + body_h - ss * 0.035
    gm = Image.new("L", (ss, ss), 0)
    ImageDraw.Draw(gm).rounded_rectangle([stand_x, stand_y, stand_x + stand_w, stand_y + stand_h],
                                         radius=stand_h * 0.42, fill=255)
    img.paste(g, (0, 0), gm)

    # 屏幕内暗底（外边距更小，屏幕更饱满）
    sc_pad = body_w * 0.085
    sc_x, sc_y = body_x + sc_pad, body_y + sc_pad
    sc_w, sc_h = body_w - sc_pad * 2, body_h - sc_pad * 2
    sfill = screen_fill(ss).convert("RGBA")
    sm = Image.new("L", (ss, ss), 0)
    ImageDraw.Draw(sm).rounded_rectangle([sc_x, sc_y, sc_x + sc_w, sc_y + sc_h],
                                         radius=body_r * 0.6, fill=255)
    img.paste(sfill, (0, 0), sm)

    # 加粗播放三角（小图占比更大）
    td = ImageDraw.Draw(img)
    _tri(td, sc_x, sc_y, sc_w, sc_h)
    return img.resize((size, size), Image.LANCZOS)


SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def make():
    frames = []
    for s, _ in SIZES:
        frames.append(draw_small(s) if s <= 32 else draw_big(s))
    frames[-1].save(OUT, format="ICO", sizes=SIZES, append_images=frames[:-1])
    print("ico ->", OUT, os.path.getsize(OUT), "bytes")

    # 预览：小尺寸放大 6x，中大尺寸放大 1.5x
    tiles = []
    for f in frames:
        s = f.width
        k = 6 if s <= 32 else (3 if s <= 64 else 1)
        tiles.append(f.resize((s * k, s * k), Image.NEAREST if s <= 32 else Image.LANCZOS))
    W = sum(t.width for t in tiles) + 16 * (len(tiles) - 1)
    H = max(t.height for t in tiles)
    cv = Image.new("RGBA", (W, H), (30, 30, 36, 255))
    x = 0
    for t in tiles:
        cv.alpha_composite(t, (x, 0))
        x += t.width + 16
    pv = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_preview_new.png")
    cv.save(pv)
    print("preview ->", pv)


if __name__ == "__main__":
    make()

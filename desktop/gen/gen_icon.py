# -*- coding: utf-8 -*-
"""生成 exe 图标（电视 + 播放键）"""
import os
from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.ico")
S = 256


def rounded(draw, box, r, fill):
    draw.rounded_rectangle(box, radius=r, fill=fill)


def make():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 背景圆角方块（深色渐变近似）
    rounded(d, (8, 8, S - 8, S - 8), 52, (20, 24, 32, 255))
    rounded(d, (8, 8, S - 8, S - 8), 52, (26, 31, 40, 255))
    # 电视外框
    rounded(d, (40, 58, S - 40, S - 74), 22, (12, 15, 20, 255))
    rounded(d, (48, 66, S - 48, S - 82), 16, (232, 236, 242, 255))
    # 屏幕内容（红色播放三角）
    cx, cy = S // 2 + 4, (58 + S - 74) // 2
    tri = [(cx - 34, cy - 42), (cx - 34, cy + 42), (cx + 46, cy)]
    d.polygon(tri, fill=(229, 57, 53, 255))
    # 天线
    d.line([(112, 58), (96, 30)], fill=(232, 236, 242, 255), width=9)
    d.line([(144, 58), (160, 30)], fill=(232, 236, 242, 255), width=9)
    # 底座
    rounded(d, (S // 2 - 34, S - 74, S // 2 + 34, S - 54), 8, (232, 236, 242, 255))
    img.save(OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("ico ->", OUT, os.path.getsize(OUT), "bytes")


if __name__ == "__main__":
    make()

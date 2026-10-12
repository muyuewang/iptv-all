# -*- coding: utf-8 -*-
"""
shots_desktop.py —— 桌面端 UI 截图取证（本机源 / 官网源 两种形态各一张）

为什么要单独写：`check_wvt_ui.py` 是**打桩**验证（断言逻辑），它不截图；
而样式统一这件事只有看图才说得清，所以这里起**真** app、真拉频道表、真切源，
再按窗口矩形抓图。

用法：
    python tools/shots_desktop.py              # 输出 _shots/ui_*.png
"""
import os
import sys
import time
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "desktop"))

OUT = os.path.join(ROOT, "_shots")
os.makedirs(OUT, exist_ok=True)

import tkinter as tk  # noqa: E402

try:
    from PIL import ImageGrab, Image  # noqa: E402
except Exception as e:  # pragma: no cover
    print("需要 Pillow：pip install pillow（%s）" % e)
    sys.exit(2)

# 复用入口模块里的 app 类（它叫 main() 的那个文件就是 native_app.py）
import native_app  # noqa: E402


def window_bbox(win):
    """取窗口在屏幕上的物理像素矩形（含标题栏，视觉上更完整）。"""
    win.update_idletasks()
    x, y = win.winfo_rootx(), win.winfo_rooty()
    w, h = win.winfo_width(), win.winfo_height()
    return (x, y, x + w, y + h)


def grab(win, name):
    win.update_idletasks()
    win.update()
    time.sleep(0.45)
    bb = window_bbox(win)
    img = ImageGrab.grab(bbox=bb, all_screens=True)
    p = os.path.join(OUT, name)
    img.save(p)
    print("  → %s  %s" % (os.path.basename(p), img.size))
    return p


def main():
    app = native_app.NativeApp()

    seq = []

    def s1():
        # 第一张必须让频道表先落地，否则左栏是空的
        print("== 1) 本机源 ==")
        app._set_src("local")
        app.root.after(3500, s2)

    def s2():
        grab(app.root, "ui_desktop_local.png")
        print("== 2) 官网源 ==")
        app._set_src("wvt")
        app.root.after(6000, s3)

    def s3():
        grab(app.root, "ui_desktop_wvt.png")
        print("== 3) 切回本机源（验证可来回）==")
        app._set_src("local")
        app.root.after(1800, s4)

    def s4():
        grab(app.root, "ui_desktop_back.png")
        app.root.after(200, lambda: app.root.destroy())

    app.root.after(1200, s1)
    app.root.mainloop()
    print("完成，产物在 _shots/")


if __name__ == "__main__":
    main()

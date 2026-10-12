# -*- coding: utf-8 -*-
"""
探针：全屏进出时，内嵌浏览器窗口的尺寸到底是谁在改。

去掉 --kiosk 之后（kiosk 会无视 --window-position 抢先全屏盖住桌面），
发现「退出全屏后内嵌窗口缩成 199×34」—— 需要分清是
  (a) 我们自己的 _fit_children 传了错的尺寸，还是
  (b) 我们改完之后 Chromium 又自己把窗口改回去了。

做法：全屏进出前后每 150ms 采一次 (host 客户区, embed 窗口矩形, emb.size)，
把时间线打出来。

用法：python tools/_probe_resize.py
"""
import os
import sys
import time
import ctypes
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "desktop"))

import embed as E  # noqa: E402
import native_app as N  # noqa: E402

u = ctypes.windll.user32

TL = []


def snap(tag):
    host = app.video_host
    hw = host.winfo_id()
    cr = wintypes.RECT()
    u.GetClientRect(hw, ctypes.byref(cr))
    cw, ch = cr.right - cr.left, cr.bottom - cr.top
    emb = app.embed
    if emb is None or not emb.hwnd:
        TL.append((tag, "无 embed", (cw, ch), None, None))
        return
    r = wintypes.RECT()
    u.GetWindowRect(emb.hwnd, ctypes.byref(r))
    TL.append((tag, "hwnd=%x" % emb.hwnd, (cw, ch),
               (r.right - r.left, r.bottom - r.top), emb.size))


def dump():
    print("\n%-26s %-14s %-14s %-14s %s" % ("时刻", "embed hwnd", "host客户区",
                                           "embed实际", "emb.size"))
    for tag, a, b, c, d in TL:
        print("%-26s %-14s %-14s %-14s %s" % (tag, a, b, c, d))


print("== 启动 ==")
app = N.NativeApp()
app.root.geometry("1200x720+60+40")
app.root.update_idletasks()
app.root.update()
app._start_mpv = lambda url, headers=None: None

STATE = {}


def step1():
    print("== 切官网源 ==")
    app._set_src("wvt")
    app.root.after(900, step2)


def step2():
    ch = None
    for c in app.wvt_channels:
        if "CCTV-13" in c["name"]:
            ch = c
            break
    print("== 打开 %s ==" % (ch and ch["name"]))
    app._play_wvt(ch["urls"][0], 0)
    app.root.after(1000, wait_play)


def wait_play():
    st = app.embed.state() if app.embed else ""
    if st == "playing":
        print("== 已起播，开始采样 ==")
        app.root.after(400, phase_window)
        return
    app.root.after(700, wait_play)


def phase_window():
    for i in range(4):
        snap("窗口态 +%.1fs" % (i * 0.15))
        time.sleep(0.15)
    app.root.update()
    print("== 进全屏 ==")
    app._set_fullscreen(True)
    app.root.after(500, phase_full)


def phase_full():
    for i in range(5):
        snap("全屏 +%.1fs" % (i * 0.15))
        time.sleep(0.15)
    app.root.update()
    print("== 退全屏 ==")
    app._set_fullscreen(False)
    app.root.after(0, phase_back)


def phase_back():
    for i in range(24):
        snap("退全屏 +%.2fs" % (i * 0.15))
        time.sleep(0.15)
        app.root.update()
    print("== 采样结束 ==")
    dump()
    try:
        app._on_close()
    except Exception:
        pass
    os._exit(0)


app.root.after(700, step1)
app.root.mainloop()

# -*- coding: utf-8 -*-
"""
探针：`--app=` 那条标题栏到底是**非客户区**还是 Chromium **画在客户区里**的。

这决定 v2.3.2 该怎么修：

  * 如果是非客户区 → `style &= ~WS_CAPTION` 就能摘掉，根本不需要 --kiosk
    （kiosk 会在播放中途重新抢全屏，把窗口从宿主里弹出去变成独立应用）。
  * 如果是客户区自绘 → 摘不掉，只能「把客户区往上挪、让标题栏被父窗口裁掉」，
    并且要让注入脚本知道自己上面被遮了 N 像素。

做法：`--app=<url>` 起一个 Edge（**不加 --kiosk**，摆在屏幕正中便于看），
量一次 window/client rect，截一张图；然后剥掉 WS_CAPTION 再量再截。

用法：python tools/_probe_appframe.py
"""
import os
import sys
import time
import ctypes
import tempfile
import subprocess
from ctypes import wintypes

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "desktop"))
import cdp  # noqa: E402
from PIL import ImageGrab  # noqa: E402

ctypes.windll.user32.SetProcessDPIAware()

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "_shots")
os.makedirs(OUT, exist_ok=True)

u = ctypes.windll.user32
CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
u.EnumWindows.argtypes = [CB, wintypes.LPARAM]
u.GetClassNameW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
u.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
u.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
u.GetWindowLongW.restype = ctypes.c_int
u.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_int]
u.SetWindowLongW.restype = ctypes.c_int
u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                           ctypes.c_int, ctypes.c_int, ctypes.c_uint]

GWL_STYLE = -16
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
SWP_FRAMECHANGED = 0x0020
SWP_NOZORDER = 0x0004


def s32(v):
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v >= 0x80000000 else v


def find_main(pid):
    res = []

    @CB
    def cb(hwnd, lp):
        p = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value != pid:
            return True
        cls = ctypes.create_unicode_buffer(128)
        u.GetClassNameW(hwnd, cls, 128)
        if not cls.value.startswith("Chrome_WidgetWin"):
            return True
        r = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))
        res.append((hwnd, (r.right - r.left) * (r.bottom - r.top)))
        return True

    u.EnumWindows(cb, 0)
    res.sort(key=lambda x: -x[1])
    return res[0][0] if res else None


def probe(hwnd, tag):
    wr = wintypes.RECT()
    cr = wintypes.RECT()
    pt = wintypes.POINT(0, 0)
    u.GetWindowRect(hwnd, ctypes.byref(wr))
    u.GetClientRect(hwnd, ctypes.byref(cr))
    u.ClientToScreen(hwnd, ctypes.byref(pt))
    ww, wh = wr.right - wr.left, wr.bottom - wr.top
    cw, ch = cr.right - cr.left, cr.bottom - cr.top
    raw = u.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF
    print("[%s] style=%08x  CAPTION=%s" % (tag, raw, bool(raw & WS_CAPTION)))
    print("      window=%dx%d  client=%dx%d  客户区偏移=(%d,%d)  非客户区=(%d,%d,%d,%d)"
          % (ww, wh, cw, ch, pt.x - wr.left, pt.y - wr.top,
             pt.x - wr.left, pt.y - wr.top, ww - cw - (pt.x - wr.left),
             wh - ch - (pt.y - wr.top)))
    img = ImageGrab.grab(bbox=(wr.left, wr.top, wr.right, wr.bottom), all_screens=True)
    p = os.path.join(OUT, "_probe_appframe_%s.png" % tag)
    img.save(p)
    print("      截图 -> %s（%s）" % (os.path.basename(p), img.size))
    return (ww, wh, cw, ch, pt.x - wr.left, pt.y - wr.top)


def main():
    exe = cdp.find_browser()
    print("browser =", exe)
    udd = tempfile.mkdtemp(prefix="appframe_")
    args = [exe, "--user-data-dir=" + udd,
            "--app=https://tv.cctv.com/live/cctv13/",
            "--no-first-run", "--no-default-browser-check", "--noerrdialogs",
            "--hide-crash-restore-bubble",
            "--window-size=900,600", "--window-position=120,120"]
    print("args =", " ".join(args[1:]))
    p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        hwnd = None
        t0 = time.time()
        while time.time() - t0 < 25 and not hwnd:
            time.sleep(0.3)
            hwnd = find_main(p.pid)
        if not hwnd:
            print("找不到窗口")
            return 2
        time.sleep(3)
        before = probe(hwnd, "before")
        print("\n== 剥掉 WS_CAPTION / WS_THICKFRAME ==")
        st = u.GetWindowLongW(hwnd, GWL_STYLE)
        u.SetWindowLongW(hwnd, GWL_STYLE, s32(st & ~(WS_CAPTION | WS_THICKFRAME)))
        u.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                       SWP_NOZORDER | SWP_FRAMECHANGED)
        time.sleep(1.5)
        after = probe(hwnd, "after")
        print()
        if after[2] == after[0] and after[3] == after[1]:
            print("→ 剥掉之后 client == window：说明那条栏是**非客户区**")
            if after[3] < before[3]:
                print("  而且客户区高度确实变小过 → 它就是标题栏占的")
        else:
            print("→ 剥掉之后 client 仍小于 window：还有非客户区残留")
        print("→ 看图确认标题栏到底还在不在（_probe_appframe_after.png）")
    finally:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

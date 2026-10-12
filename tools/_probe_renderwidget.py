# -*- coding: utf-8 -*-
"""
探针：Chromium 窗口里有没有一个「网页渲染子窗口」，它的位置能不能直接量出
「客户区顶部 → 网页内容顶端」的偏移量（也就是那条自绘标题栏的高度）。

为什么需要：
  保留 WS_CAPTION 也没用 —— 一旦窗口变成 WS_CHILD，Windows 会**忽略 caption**，
  `client_insets()` 上边仍然是 0（实测 (左8, 上0, 右8, 下8)），
  于是 Chromium 继续在客户区里自绘那条标题栏，父窗口裁不掉。

  但如果 Chromium 内部把网页渲染在一个**真正的子 HWND**里
  （`Chrome_RenderWidgetHostHWND`），那么「父客户区原点 → 该子窗口原点」的差值
  就是那条栏的精确厚度，且天然跟随 DPI / Edge 版本，不用写死 38px。

做法：起一个 `--app=` 窗口 → 找主窗口 → 枚举它所有子窗口，打印类名 / 矩形 /
相对客户区原点的偏移。

用法：python tools/_probe_renderwidget.py [url]
"""
import os
import sys
import time
import ctypes
import tempfile
import subprocess
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "desktop"))

import cdp          # noqa: E402
import embed as E   # noqa: E402

ctypes.windll.user32.SetProcessDPIAware()
u = ctypes.windll.user32
u.GetClassNameW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
u.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
u.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
u.IsWindowVisible.argtypes = [wintypes.HWND]
u.IsWindowVisible.restype = wintypes.BOOL
_CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
u.EnumChildWindows.argtypes = [wintypes.HWND, _CB, wintypes.LPARAM]

URL = sys.argv[1] if len(sys.argv) > 1 else "https://tv.cctv.com/live/cctv2/"


def screen_of(hwnd, cx=0, cy=0):
    pt = wintypes.POINT(cx, cy)
    u.ClientToScreen(hwnd, ctypes.byref(pt))
    return (pt.x, pt.y)


def dump(hwnd, label):
    cr = wintypes.RECT()
    u.GetClientRect(hwnd, ctypes.byref(cr))
    c0 = screen_of(hwnd, 0, 0)
    print("  [%s] hwnd=%x 客户区=%dx%d 客户区屏幕原点=%s"
          % (label, hwnd, cr.right - cr.left, cr.bottom - cr.top, c0))
    rows = []

    @_CB
    def cb(h, lp):
        if h == hwnd:
            return True
        cls = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(h, cls, 256)
        r = wintypes.RECT()
        u.GetWindowRect(h, ctypes.byref(r))
        # 子窗口的客户区原点相对**父客户区**的偏移
        # 父客户区原点(屏幕) - 子窗口矩形左上(屏幕) 的负值 = 子窗口相对父客户区的偏移
        ox, oy = r.left - c0[0], r.top - c0[1]
        cw, ch = r.right - r.left, r.bottom - r.top
        rows.append((cls.value, h, ox, oy, cw, ch,
                     bool(u.IsWindowVisible(h))))
        return True

    u.EnumChildWindows(hwnd, cb, 0)
    rows.sort(key=lambda t: (t[3], t[2]))
    for cls, h, ox, oy, cw, ch, vis in rows:
        mark = ""
        if "RenderWidget" in cls or "Intermediate" in cls:
            mark = "   <<< 网页渲染子窗口"
        print("    %-32s hwnd=%x 相对父客户区=(%4d,%4d) 尺寸=%dx%d 可见=%s%s"
              % (cls, h, ox, oy, cw, ch, vis, mark))
    return rows


def main():
    exe = cdp.find_browser()
    if not exe:
        print("没找到 Edge/Chrome")
        return 2
    udd = tempfile.mkdtemp(prefix="probe_rwh_")
    args = [
        exe, "--remote-debugging-port=19733", "--user-data-dir=" + udd,
        "--app=" + URL, "--no-first-run", "--no-default-browser-check",
        "--noerrdialogs", "--hide-crash-restore-bubble",
        "--window-size=1000,700", "--window-position=150,120",
    ]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        hwnd = E.find_hwnd(proc.pid, timeout=30)
        if not hwnd:
            print("找不到主窗口")
            return 2
        print("主窗口 hwnd=%x" % hwnd)
        time.sleep(8)
        print()
        print("=== 内嵌前（顶层窗口，带 caption）===")
        print("  client_insets =", E.client_insets(hwnd))
        dump(hwnd, "before")

        print()
        print("=== 改成 WS_CHILD（模拟 attach：只去 WS_POPUP，保留 caption）===")
        st = u.GetWindowLongW(hwnd, E.GWL_STYLE)
        u.SetWindowLongW(hwnd, E.GWL_STYLE, E._s32((st & ~E.WS_POPUP) | E.WS_CHILD))
        u.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                       0x0002 | 0x0001 | E.SWP_NOZORDER |
                       E.SWP_FRAMECHANGED | E.SWP_SHOWWINDOW)
        time.sleep(3)
        print("  client_insets =", E.client_insets(hwnd))
        dump(hwnd, "child")

        print()
        print("怎么看：如果有个 Chrome_RenderWidgetHostHWND，并且它在 child 状态下")
        print("  相对父客户区=(9, 38) 之类，那 38 就是那条自绘标题栏的精确高度 ——")
        print("  fit_child_to 用它（而不是 client_insets）对齐，就能把栏整个裁到播放区外。")
    finally:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

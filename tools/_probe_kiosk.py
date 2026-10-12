# -*- coding: utf-8 -*-
"""
探针：浏览器在「被 SetParent 嵌进去之前」到底会不会露脸。

背景：embed.py 的时序是
    ensure()（起进程） → _connect()（等 CDP，可能 1~5s） → attach()（才去 SetParent）
这中间浏览器窗口已经存在了。如果它是可见的（尤其 --kiosk 是全屏模式），
用户就会看到「莫名打开了一个应用」。

本脚本用**完全一样**的启动参数起一个 Edge，什么都不改，只在 0.5/1/2/3/5 秒
各抓一张全屏图，看窗口是不是已经铺在屏幕上了。

用法：python tools/_probe_kiosk.py [--no-kiosk]
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

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "_shots")
os.makedirs(OUT, exist_ok=True)

USE_KIOSK = "--no-kiosk" not in sys.argv

u = ctypes.windll.user32
u.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
                          wintypes.LPARAM]
u.GetWindowTextW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
u.GetClassNameW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
u.IsWindowVisible.argtypes = [wintypes.HWND]
u.IsWindowVisible.restype = wintypes.BOOL
u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]


def chrome_windows(pid=None):
    """返回 [(hwnd, class, title, rect, visible)]，只要 Chrome_WidgetWin*。

    ★ pid 必须过滤：这台机器上 WorkBuddy / QQ / Clash / Qoder 全是 Electron，
      圆一抓一大把 Chrome_WidgetWin_1，不过滤根本看不出哪个是我们的。
    """
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, lp):
        cls = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, cls, 256)
        if not cls.value.startswith("Chrome_WidgetWin"):
            return True
        if pid is not None:
            p = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
            if p.value != pid:
                return True
        r = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))
        t = ctypes.create_unicode_buffer(256)
        u.GetWindowTextW(hwnd, t, 256)
        out.append((hwnd, cls.value, t.value,
                    (r.left, r.top, r.right, r.bottom),
                    bool(u.IsWindowVisible(hwnd))))
        return True

    u.EnumWindows(cb, 0)
    return out


def main():
    exe = cdp.find_browser()
    print("browser =", exe)
    if not exe:
        return 1
    udd = tempfile.mkdtemp(prefix="kiosk_probe_")
    port = cdp.free_port(19800)
    args = [exe, "--remote-debugging-port=%d" % port,
            "--user-data-dir=" + udd,
            "--app=https://tv.cctv.com/live/cctv13/",
            "--no-first-run", "--no-default-browser-check",
            "--noerrdialogs", "--hide-crash-restore-bubble",
            "--window-size=1280,720", "--window-position=-32000,-32000"]
    if USE_KIOSK:
        args.insert(4, "--kiosk")
    print("kiosk =", USE_KIOSK)
    print("args  =", " ".join(args[1:]))
    p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.time()
    try:
        for t in (0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0):
            dt = t - (time.time() - t0)
            if dt > 0:
                time.sleep(dt)
            ws = chrome_windows(p.pid)
            img = ImageGrab.grab(all_screens=True)
            name = "_probe_kiosk_%s_%04d.png" % ("on" if USE_KIOSK else "off", int(t * 1000))
            img.save(os.path.join(OUT, name))
            print("t=%5.1fs  我们进程的浏览器窗口数=%d" % (t, len(ws)))
            for hwnd, cls, title, rect, vis in ws:
                print("          hwnd=%x 可见=%-5s rect=%s  %s" % (hwnd, vis, rect, title[:36]))
            print("          截图 -> %s" % name)
    finally:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return 0


if __name__ == "__main__":
    sys.exit(main())

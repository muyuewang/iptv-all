# -*- coding: utf-8 -*-
"""真实频道播放截图：CCTV-1 官网源，PrintWindow 抓主窗口。"""
import os
import sys
import time
import ctypes

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "desktop"))
sys.path.insert(0, ROOT)
os.chdir(os.path.join(ROOT, "desktop"))

try:
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass

import native_app as na          # noqa: E402

_u32 = ctypes.windll.user32
_g32 = ctypes.windll.gdi32
OUT = os.path.join(ROOT, "_shots", "wv2_live_cctv1.png")


def grab_root(hwnd, path):
    r = ctypes.wintypes.RECT()
    _u32.GetWindowRect(hwnd, ctypes.byref(r))
    W, H = r.right - r.left, r.bottom - r.top
    hDC = _u32.GetWindowDC(hwnd)
    memDC = _g32.CreateCompatibleDC(hDC)
    hbmp = _g32.CreateCompatibleBitmap(hDC, W, H)
    _g32.SelectObject(memDC, hbmp)
    _u32.PrintWindow(hwnd, memDC, 2)

    class BI(ctypes.Structure):
        _fields_ = [("biSize", ctypes.wintypes.DWORD),
                    ("biWidth", ctypes.wintypes.LONG),
                    ("biHeight", ctypes.wintypes.LONG),
                    ("biPlanes", ctypes.wintypes.WORD),
                    ("biBitCount", ctypes.wintypes.WORD),
                    ("biCompression", ctypes.wintypes.DWORD),
                    ("biSizeImage", ctypes.wintypes.DWORD),
                    ("biXPelsPerMeter", ctypes.wintypes.LONG),
                    ("biYPelsPerMeter", ctypes.wintypes.LONG),
                    ("biClrUsed", ctypes.wintypes.DWORD),
                    ("biClrImportant", ctypes.wintypes.DWORD)]
    bi = BI()
    bi.biSize = ctypes.sizeof(BI)
    bi.biWidth = W
    bi.biHeight = -H
    bi.biPlanes = 1
    bi.biBitCount = 32
    buf = ctypes.create_string_buffer(W * H * 4)
    _g32.GetDIBits(memDC, hbmp, 0, H, buf, ctypes.byref(bi), 0)
    from PIL import Image
    Image.frombuffer("RGB", (W, H), buf.raw, "raw", "BGRX", 0, 1).save(path)
    print("已存:", path, "%dx%d" % (W, H))


def main():
    app = na.NativeApp()
    state = {"phase": 0}

    def step():
        try:
            _wvt = na._wvt
            chans = _wvt.load_channels(cache_dir=".")
            ads = _wvt.load_adapters(cache_dir=".")
            app._after_wvt_load(chans, ads)
        except Exception as e:
            print("预载失败:", e)
        # 切官网源 → 打开 CCTV-1
        app._set_src("wvt")
        app.root.after(500, open_ch)

    def open_ch():
        chs = app.wvt_channels
        c = next((x for x in chs if x["name"].startswith("广东卫视")
                  and True), chs[0])
        print("打开频道:", c["name"], (c.get("urls") or [""])[0][:60])
        app._open_channel(c.get("tid", "wvt"), c["id"], c["name"])
        app.root.after(30000, shot)

    def shot():
        try:
            grab_root(app.root.winfo_id(), OUT)
            st = app.embed.state() if app.embed else "?"
            print("页面状态:", st)
        except Exception as e:
            print("截图失败:", e)
        app.root.after(200, app.root.destroy)

    app.root.after(1200, step)
    app.root.mainloop()


if __name__ == "__main__":
    os.makedirs(os.path.join(ROOT, "_shots"), exist_ok=True)
    main()

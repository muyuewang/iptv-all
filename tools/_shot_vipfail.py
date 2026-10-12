# -*- coding: utf-8 -*-
"""v2.4.6 失败反馈验证：
① CCTV-1（免费）应正常起播 —— 站点错误词表不能误杀；
② CCTV-世界地理（VIP 专享，单线路）应在数秒内报失败并带出原因，
   播放区出现明确文字提示，而不是黑屏干等 20s+嗅探 30s。
PrintWindow 抓主窗口截图 + 打印状态时间线。
"""
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
_SHOTS = os.path.join(ROOT, "_shots")


def grab_root(hwnd, path):
    import ctypes.wintypes as wt
    r = wt.RECT()
    _u32.GetWindowRect(hwnd, ctypes.byref(r))
    W, H = r.right - r.left, r.bottom - r.top
    hDC = _u32.GetWindowDC(hwnd)
    memDC = _g32.CreateCompatibleDC(hDC)
    hbmp = _g32.CreateCompatibleBitmap(hDC, W, H)
    _g32.SelectObject(memDC, hbmp)
    _u32.PrintWindow(hwnd, memDC, 2)

    class BI(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG),
                    ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                    ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                    ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                    ("biClrImportant", wt.DWORD)]
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
    t0 = time.time()
    log = []

    def mark(s):
        log.append("[%5.1fs] %s" % (time.time() - t0, s))
        print(log[-1], flush=True)

    def status():
        try:
            return app.status_lbl.cget("text")
        except Exception:
            return "?"

    def step_load():
        try:
            _wvt = na._wvt
            chans = _wvt.load_channels(cache_dir=".")
            ads = _wvt.load_adapters(cache_dir=".")
            app._after_wvt_load(chans, ads)
        except Exception as e:
            print("预载失败:", e)
        app._set_src("wvt")
        app.root.after(500, open_free)

    # ---------- ① CCTV-1：不误杀 ----------
    def open_free():
        chs = app.wvt_channels
        c = next(x for x in chs if x["name"] == "CCTV-1 综合")
        mark("打开免费频道 " + c["name"])
        app._open_channel(c.get("tid", "wvt"), c["id"], c["name"])
        app.root.after(1000, watch_free)

    def watch_free():
        st = app._web_state
        if st == "playing":
            mark("CCTV-1 playing ✔（未误杀）")
            grab_root(app.root.winfo_id(),
                      os.path.join(_SHOTS, "fail_cctv1_playing.png"))
            app.root.after(600, open_vip)
            return
        if time.time() - t0 > 75:
            mark("CCTV-1 未起播 ✘（状态=%s 状态栏=%s）" % (st, status()))
            grab_root(app.root.winfo_id(),
                      os.path.join(_SHOTS, "fail_cctv1_stuck.png"))
            app.root.after(300, open_vip)
            return
        app.root.after(1000, watch_free)

    # ---------- ② CCTV-世界地理：VIP 快速反馈 ----------
    def open_vip():
        chs = app.wvt_channels
        c = next(x for x in chs if "世界地理" in x["name"])
        mark("打开VIP频道 " + c["name"] + "（线路数 %d）" % len(c["urls"]))
        tv = time.time()
        app._open_channel(c.get("tid", "wvt"), c["id"], c["name"])
        watch_vip(tv)

    def watch_vip(tv):
        st = app._web_state
        txt = status()
        if st == "" and app._load_frame and not app._load_frame.winfo_ismapped() \
                and "不可用" in txt:
            done_vip(tv, txt)
            return
        if "不可用" in txt or time.time() - tv > 60:
            done_vip(tv, txt)
            return
        app.root.after(1000, lambda: watch_vip(tv))

    def done_vip(tv, txt):
        mark("VIP 频道收工（耗时 %.1fs）状态栏=%s" % (time.time() - tv, txt))
        grab_root(app.root.winfo_id(),
                  os.path.join(_SHOTS, "fail_vip_feedback.png"))
        app.root.after(300, app.root.destroy)

    app.root.after(1200, step_load)
    try:
        app.root.mainloop()
    finally:
        print("\n----- 时间线 -----")
        for l in log:
            print(l)


if __name__ == "__main__":
    os.makedirs(_SHOTS, exist_ok=True)
    main()

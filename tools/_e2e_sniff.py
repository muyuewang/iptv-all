# -*- coding: utf-8 -*-
"""全链路 e2e：网页线路全败 → 嗅探兜底 → mpv 起播。

观测：mpv 进程存活 + IPC 连接 + 窗口截图。
用法：python _e2e_sniff.py [频道名前缀] [时长秒]
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

import native_app as na  # noqa: E402

_u32 = ctypes.windll.user32
_g32 = ctypes.windll.gdi32


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
        _fields_ = [(n, getattr(ctypes, t)) for n, t in [
            ("biSize", "c_uint32"), ("biWidth", "c_int32"),
            ("biHeight", "c_int32"), ("biPlanes", "c_uint16"),
            ("biBitCount", "c_uint16"), ("biCompression", "c_uint32"),
            ("biSizeImage", "c_uint32"), ("biXPelsPerMeter", "c_int32"),
            ("biYPelsPerMeter", "c_int32"), ("biClrUsed", "c_uint32"),
            ("biClrImportant", "c_uint32")]]
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
    return path


def main():
    want = sys.argv[1] if len(sys.argv) > 1 else "广东卫视"
    dur = int(sys.argv[2]) if len(sys.argv) > 2 else 150
    app = na.NativeApp()
    finished = {"v": False}

    def pick():
        chs = app.wvt_channels
        return next((x for x in chs if x["name"].startswith(want)),
                    chs[0] if chs else None)

    def step():
        try:
            _wvt = na._wvt
            chans = _wvt.load_channels(cache_dir=".")
            ads = _wvt.load_adapters(cache_dir=".")
            app._after_wvt_load(chans, ads)
        except Exception as e:
            print("预载失败:", e)
        app._set_src("wvt")
        app.root.after(500, open_ch)

    def open_ch():
        c = pick()
        if not c:
            print("找不到频道", want); finished["v"] = True; return
        print("打开频道:", c["name"], flush=True)
        app._open_channel(c.get("tid", "wvt"), c["id"], c["name"])
        import threading
        threading.Thread(target=watch_bg, args=(c["name"],), daemon=True).start()

    def watch_bg(name):
        t0 = time.time()
        mpv_seen = False
        shot_path = os.path.join(ROOT, "_shots", "e2e_sniff.png")
        while time.time() - t0 < dur:
            time.sleep(5)
            p = app.mpv_proc
            alive = p is not None and p.poll() is None
            st = "mpv=存活" if alive else "mpv=无"
            if alive:
                mpv_seen = True
            ipc = "ipc=通" if (app.ipc and getattr(app.ipc, "conn", None)) \
                else "ipc=未连"
            print("T+%03ds %s %s" % (time.time() - t0, st, ipc), flush=True)
            if mpv_seen and alive:
                # mpv 起来后再等 8s 让画面出来，然后截图收工
                if time.time() - t0 > 0:
                    time.sleep(8)
                    try:
                        os.makedirs(os.path.join(ROOT, "_shots"), exist_ok=True)
                        grab_root(app.root.winfo_id(), shot_path)
                        print("已截图:", shot_path, flush=True)
                    except Exception as e:
                        print("截图失败:", e, flush=True)
                finished["v"] = True
                return
        print("超时（%ds 内 mpv 未起播）" % dur, flush=True)
        finished["v"] = True

    def check():
        if finished["v"]:
            app.root.after(200, app.root.destroy)
        else:
            app.root.after(300, check)

    app.root.after(1200, step)
    app.root.after(1500, check)
    app.root.mainloop()


if __name__ == "__main__":
    main()

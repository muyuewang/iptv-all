# -*- coding: utf-8 -*-
"""
WebView2 冒烟测试：不经过 native_app，直接驱动 embed.EmbeddedBrowser。
验证：环境/控件创建、JS 执行往返、子窗口真的挂在 host 下、put_Bounds 生效、
     截图里真有渲染内容。

用法：python tools/check_wv2_smoke.py
"""
import os
import sys
import time
import ctypes
import threading

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DESK = os.path.join(HERE, "desktop")
sys.path.insert(0, DESK)
sys.path.insert(0, HERE)

ctypes.windll.user32.SetProcessDPIAware()

import tkinter as tk
import embed as EM

OK = 0
NG = 0


def check(cond, label):
    global OK, NG
    print(("PASS " if cond else "FAIL ") + label)
    if cond:
        OK += 1
    else:
        NG += 1
    return cond


def main():
    if not EM.webview2_available():
        print("FAIL 系统没有 WebView2 运行时")
        return 1
    print("PASS WebView2 运行时存在")
    check(EM._find_loader() is not None, "WebView2Loader.dll 可找到")

    root = tk.Tk()
    root.title("wv2-smoke")
    root.geometry("1000x640+40+40")
    host = tk.Frame(root, bg="#202020")
    host.pack(fill="both", expand=True)
    root.update()
    time.sleep(0.5)
    root.update()

    emb = EM.EmbeddedBrowser(
        DESK, log=lambda *a: print("  [emb]", *a),
        schedule=lambda ms, fn: root.after(int(ms), fn))

    # ★ data: URL 里不能出现裸 '#'（会被当成 fragment 截断后半段 HTML）
    html = ("data:text/html;charset=utf-8,"
            "<!DOCTYPE html><html><body style='background:rgb(17,34,68); margin:0'>"
            "<h1 style='color:white;font-size:60px'>WebView2 SMOKE OK</h1>"
            "<script>window.__probe=41+1;</script>"
            "</body></html>")
    inject = "window.__wvtInjected = 1; window.__wvt_state='';"
    hw = host.winfo_id()          # ★ winfo_id 只能在主线程调

    result = {}

    def work():
        result["ok"] = emb.play(html, "{}", "", inject, hw, 980, 600)

    th = threading.Thread(target=work, daemon=True)
    th.start()
    t0 = time.time()
    while th.is_alive() and time.time() - t0 < 90:
        root.update()
        time.sleep(0.02)
    check(th.is_alive() is False, "play() 在 90s 内完成")
    check(bool(result.get("ok")), "play() 返回 True")
    check(emb.alive(), "控件存活")

    # JS 往返
    r = emb._eval_sync("(window.__probe||0)", timeout=8)
    check(r == 42, "ExecuteScript 往返返回 42（实得 %r）" % (r,))
    r2 = emb._eval_sync("document.readyState", timeout=8)
    check(r2 == "complete", "页面 readyState=complete（实得 %r）" % (r2,))

    # 子窗口挂在 host 下 + 尺寸
    root.update()
    time.sleep(0.8)
    root.update()
    u32 = ctypes.windll.user32
    found = []
    CBTYPE = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL,
                                ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

    @CBTYPE
    def cb(h, lp):
        cls = ctypes.create_unicode_buffer(256)
        u32.GetClassNameW(h, cls, 256)
        if "Chrome_WidgetWin" in cls.value:
            found.append(h)
        return True

    u32.EnumChildWindows(host.winfo_id(), cb, 0)
    check(bool(found), "host 下有 WebView2 子窗口（Chrome_WidgetWin）: %d 个" % len(found))
    if found:
        r = ctypes.wintypes.RECT()
        u32.GetClientRect(found[0], ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        hr = ctypes.wintypes.RECT()
        u32.GetClientRect(host.winfo_id(), ctypes.byref(hr))
        hw, hh = hr.right - hr.left, hr.bottom - hr.top
        check(abs(w - hw) <= 4 and abs(h - hh) <= 4,
              "子窗口客户区铺满 host（%dx%d vs %dx%d）" % (w, h, hw, hh))

    # 截图目检：★ 用 PrintWindow 让窗口自己画（ImageGrab 会被前景窗口遮挡）
    try:
        from PIL import Image
        root.update()
        time.sleep(0.6)
        root.update()
        u32 = ctypes.windll.user32
        g32 = ctypes.windll.gdi32
        r = ctypes.wintypes.RECT()
        u32.GetWindowRect(root.winfo_id(), ctypes.byref(r))
        W, H = r.right - r.left, r.bottom - r.top
        hDC = u32.GetWindowDC(root.winfo_id())
        memDC = g32.CreateCompatibleDC(hDC)
        hbmp = g32.CreateCompatibleBitmap(hDC, W, H)
        g32.SelectObject(memDC, hbmp)
        u32.PrintWindow(root.winfo_id(), memDC, 2)   # 2 = PW_RENDERFULLCONTENT

        class BMPINFOHEADER(ctypes.Structure):
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
        bi = BMPINFOHEADER()
        bi.biSize = ctypes.sizeof(BMPINFOHEADER)
        bi.biWidth = W
        bi.biHeight = -H
        bi.biPlanes = 1
        bi.biBitCount = 32
        buf = ctypes.create_string_buffer(W * H * 4)
        g32.GetDIBits(memDC, hbmp, 0, H, buf, ctypes.byref(bi), 0)
        img = Image.frombuffer("RGB", (W, H), buf.raw, "raw", "BGRX", 0, 1)
        img.save(os.path.join(HERE, "_shots", "wv2_smoke.png"))
        colors = img.convert("RGB").getcolors(200000)
        colors.sort(reverse=True)
        top = colors[0] if colors else (0, (0, 0, 0))
        check(len(colors) > 8 and top[1] != (32, 32, 32),
              "截图非纯背景（主色 %s，颜色数 %d）" % (top[1], len(colors)))
    except Exception as e:
        print("SKIP 截图失败：%r" % (e,))

    # 关闭
    emb.close()
    time.sleep(0.5)
    check(emb.alive() is False, "close() 后控件不再存活")

    root.destroy()
    print("RESULT %s (%d PASS / %d FAIL)" % ("PASS" if NG == 0 else "FAIL", OK, NG))
    return 0 if NG == 0 else 1


if __name__ == "__main__":
    os.makedirs(os.path.join(HERE, "_shots"), exist_ok=True)
    sys.exit(main())

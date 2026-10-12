# -*- coding: utf-8 -*-
"""
探针 v2：判定「播放区顶上那条栏」是**网页内容**还是**浏览器自绘窗口框**。

判据（一锤定音）：
  * `Page.captureScreenshot` 抓的是**纯网页渲染**，不含任何浏览器 UI。
  * 把「窗口矩形截图」和「网页截图」并排看：
      网页截图里也有那条栏  → 它是**网页自己的 header**（我们管不着，只能靠注入脚本盖掉）
      网页截图里没有那条栏  → 它是**浏览器窗口框**（剥 WS_CAPTION / 用客户区对齐裁掉）
  * 同时打印 `client_insets()`：top==38 → 那条栏在非客户区（原生标题栏）；
    top==0 而栏还在 → 它画在客户区里。

★ 每次用**全新 profile 目录**：复用 profile 时 Chromium 会把请求转给上一次残留的
  实例，我们量到的其实是那个老窗口（上一版探针就因为这个自相矛盾：一次 top=38、
  一次 top=0）。

用法：python tools/_probe_frame2.py [url]
"""
import os
import sys
import json
import time
import base64
import ctypes
import tempfile
import subprocess
from ctypes import wintypes

from PIL import ImageGrab

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "desktop"))

import cdp          # noqa: E402
import embed as E   # noqa: E402

SHOT = os.path.join(ROOT, "_shots")
os.makedirs(SHOT, exist_ok=True)
ctypes.windll.user32.SetProcessDPIAware()
u = ctypes.windll.user32

URL = sys.argv[1] if len(sys.argv) > 1 else "https://tv.cctv.com/live/cctv2/"
PORT = 19817


def rect(hwnd):
    r = wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def main():
    exe = cdp.find_browser()
    if not exe:
        print("没找到 Edge/Chrome")
        return 2
    udd = tempfile.mkdtemp(prefix="probe_frame2_")
    print("profile:", udd)
    args = [
        exe, "--remote-debugging-port=%d" % PORT, "--user-data-dir=" + udd,
        "--app=" + URL, "--no-first-run", "--no-default-browser-check",
        "--noerrdialogs", "--hide-crash-restore-bubble",
        "--autoplay-policy=no-user-gesture-required",
        "--disable-features=CalculateNativeWinOcclusion",
        "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
        "--window-size=900,600", "--window-position=200,150",
    ]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        hwnd = E.find_hwnd(proc.pid, timeout=30)
        if not hwnd:
            print("找不到窗口")
            return 2
        print("hwnd = %x  pid = %d" % (hwnd, proc.pid))
        time.sleep(8)                        # 等页面把标题栏/header 渲染出来

        for tag, strip in (("A_with_caption", False), ("B_no_caption", True)):
            if strip:
                style = u.GetWindowLongW(hwnd, E.GWL_STYLE)
                u.SetWindowLongW(hwnd, E.GWL_STYLE, E._s32(
                    style & ~(E.WS_POPUP | E.WS_CAPTION | E.WS_THICKFRAME |
                              E.WS_SYSMENU | E.WS_MINIMIZE | E.WS_MAXIMIZE)
                    | E.WS_CHILD))
                u.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                               0x0002 | 0x0001 | E.SWP_NOZORDER |
                               E.SWP_FRAMECHANGED | E.SWP_SHOWWINDOW)   # NOMOVE|NOSIZE
                time.sleep(3)
            print()
            print("===== %s =====" % tag)
            print("  client_insets =", E.client_insets(hwnd))
            r = rect(hwnd)
            print("  window rect   =", r, "尺寸 %dx%d" % (r[2] - r[0], r[3] - r[1]))
            p1 = os.path.join(SHOT, "_probe_frame2_%s_window.png" % tag)
            ImageGrab.grab(bbox=r, all_screens=True).save(p1)
            print("  窗口截图:", p1)

            # —— 纯网页渲染截图 ——
            try:
                pages = cdp.wait_targets(PORT, timeout=8)
                ws = cdp.MiniWS(pages[0]["webSocketDebuggerUrl"], timeout=30)
                ws.call("Page.enable", {})
                res = ws.call("Page.captureScreenshot", {"format": "png", "fromSurface": True})
                raw = (res.get("result") or {}).get("data") or ""
                if raw:
                    p2 = os.path.join(SHOT, "_probe_frame2_%s_page.png" % tag)
                    with open(p2, "wb") as f:
                        f.write(base64.b64decode(raw))
                    print("  网页截图:", p2)
                else:
                    print("  网页截图: 失败（无数据）")
                try:
                    ws.close()
                except Exception:
                    pass
            except Exception as e:
                print("  网页截图失败：%r" % (e,))
    finally:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

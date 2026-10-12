# -*- coding: utf-8 -*-
"""
探针：播放区顶上那条「CCTV-x…频道高清直播_CCTV节目官网_央视网   −」到底是哪种？

两条互斥的假设：
  (a) 它是**非客户区的原生标题栏** → 剥掉 `WS_CAPTION` 就该消失；
      而且 `client_insets()` 的 top 会等于那条栏的高度，父窗口 `WS_CLIPCHILDREN`
      把它裁掉即可（`fit_child_to` 现在就是这么干的）。
  (b) 它是 **Chromium 画在客户区里的自绘条** → 剥掉 `WS_CAPTION` 反而会**激**出它
      （Chromium 发现没有系统边框了就自己画一条）；此时 `client_insets()` 的 top
      会是 0，可视觉上那条栏还在，`fit_child_to` 就完全没辙了。

判据 —— 同时量两件事：
  * `client_insets(hwnd).top`（几何）
  * 窗口矩形截图里，从顶部数第几行开始才是**网页内容**（视觉）
  top == 视觉栏高  → (a)；top == 0 而视觉栏高 ≈ 38 → (b)。

用法：python tools/_probe_caption.py [url]
"""
import os
import sys
import time

import ctypes
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
POS = (200, 150)
SIZE = (900, 600)


def rect(hwnd):
    r = wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def snap(hwnd, tag):
    r = rect(hwnd)
    p = os.path.join(SHOT, "_probe_caption_%s.png" % tag)
    ImageGrab.grab(bbox=r, all_screens=True).save(p)
    return p, r


def main():
    exe = cdp.find_browser()
    if not exe:
        print("没找到 Edge/Chrome")
        return 2
    import subprocess
    udd = os.path.join(ROOT, "_shots", ".probe_caption_profile")
    E.reset_profile_crash_state(udd)
    args = [
        exe,
        "--remote-debugging-port=%d" % 19999,
        "--user-data-dir=" + udd,
        "--app=" + URL,
        "--no-first-run", "--no-default-browser-check", "--noerrdialogs",
        "--hide-crash-restore-bubble",
        "--window-size=%d,%d" % SIZE,
        "--window-position=%d,%d" % POS,
    ]
    print("启动:", " ".join(args[1:4]), URL)
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        hwnd = E.find_hwnd(proc.pid, timeout=30)
        if not hwnd:
            print("找不到窗口")
            return 2
        time.sleep(6)                      # 等页面把标题渲染出来

        print()
        print("========== 状态 A：原样（带 WS_CAPTION）==========")
        print("  client_insets =", E.client_insets(hwnd))
        print("  window rect   =", rect(hwnd))
        p, _ = snap(hwnd, "A_with_caption")
        print("  截图:", p)

        print()
        print("========== 状态 B：剥掉 WS_CAPTION ==========")
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        style = u.GetWindowLongW(hwnd, E.GWL_STYLE)
        u.SetWindowLongW(hwnd, E.GWL_STYLE, E._s32(
            style & ~(E.WS_POPUP | E.WS_CAPTION | E.WS_THICKFRAME | E.WS_SYSMENU |
                      E.WS_MINIMIZE | E.WS_MAXIMIZE) | E.WS_CHILD))
        # 注意：这里故意**不** SetParent，只看样式本身对边框的影响
        u.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                       SWP_NOMOVE | SWP_NOSIZE | E.SWP_NOZORDER |
                       E.SWP_FRAMECHANGED | E.SWP_SHOWWINDOW)
        time.sleep(2.5)
        print("  client_insets =", E.client_insets(hwnd))
        print("  window rect   =", rect(hwnd))
        p, _ = snap(hwnd, "B_no_caption")
        print("  截图:", p)

        print()
        print("结论怎么看：")
        print("  状态 A 的 client_insets.top ≈ 38  → 那条是**原生非客户区标题栏**")
        print("  状态 A 的 top == 0 且截图里顶上仍有 ~38px 的栏 → **Chromium 自绘**")
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        import subprocess as sp
        sp.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
               stdout=sp.DEVNULL, stderr=sp.DEVNULL)
    return 0


if __name__ == "__main__":
    sys.exit(main())

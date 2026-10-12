# -*- coding: utf-8 -*-
"""
探针：按 embed.ensure() 的**完全相同**启动参数起浏览器（含 --window-position=-32000,-32000），
逐秒打印「样式位 / 窗口矩形 / 客户区矩形 / 内缩值」，并对比 SW_SHOWNA 前后。

要回答的问题：
  为什么 exe 里在 SetParent 之前量到的非客户区是 (0,0,0,0)，
  而 _probe_frame2.py（窗口摆在屏幕正中、可见）量到的是 (9,38,9,9)？

候选原因：
  a) 离屏 + 从未显示过 → Windows 不算非客户区
  b) 窗口根本没有 WS_CAPTION（Chromium 对离屏 app 窗口换了个样式）
  c) find_hwnd 挑到的不是带 caption 的那个窗口
（b)、(c) 看样式位就知道；(a) 看 SW_SHOWNA 之后有没有变化。

用法：python tools/_probe_hiddenframe.py [url]
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
URL = sys.argv[1] if len(sys.argv) > 1 else "https://tv.cctv.com/live/cctv13/"
W, H = 856, 588

STYLE_BITS = [
    ("POPUP", 0x80000000), ("CHILD", 0x40000000), ("CAPTION", 0x00C00000),
    ("THICKFRAME", 0x00040000), ("SYSMENU", 0x00080000),
    ("MINIMIZE", 0x20000000), ("MAXIMIZE", 0x01000000), ("VISIBLE", 0x10000000),
]


def describe(tag):
    hwnd = hwnds[0] if hwnds else None


def _rect_of(hwnd):
    r = wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def row(hwnd, tag):
    st = u.GetWindowLongW(hwnd, E.GWL_STYLE)
    st = E._s32(st)
    bits = "|".join(n for n, v in STYLE_BITS if st & v) or "-"
    wr = wintypes.RECT()
    cr = wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(wr))
    u.GetClientRect(hwnd, ctypes.byref(cr))
    print("  %-22s style=%08x [%s] 窗口=%dx%d 客户区=%dx%d 内缩=%s 可见=%s"
          % (tag, st & 0xFFFFFFFF, bits,
             wr.right - wr.left, wr.bottom - wr.top,
             cr.right - cr.left, cr.bottom - cr.top,
             E.client_insets(hwnd), bool(u.IsWindowVisible(hwnd))))


hwnds = []


def main():
    exe = cdp.find_browser()
    if not exe:
        print("没找到 Edge/Chrome")
        return 2
    udd = tempfile.mkdtemp(prefix="probe_hidden_")
    # ↓↓ 与 embed.ensure() 完全一致的启动参数（只少一些无关开关）↓↓
    args = [
        exe, "--remote-debugging-port=19655", "--user-data-dir=" + udd,
        "--app=" + URL,
        "--no-first-run", "--no-default-browser-check", "--disable-sync",
        "--disable-breakpad", "--disable-component-update", "--no-service-autorun",
        "--password-store=basic", "--disable-popup-blocking",
        "--noerrdialogs", "--hide-crash-restore-bubble",
        "--autoplay-policy=no-user-gesture-required",
        "--disable-features=Translate,MediaRouter,CalculateNativeWinOcclusion",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-background-timer-throttling",
        "--disable-ipc-flooding-protection",
        "--window-size=%d,%d" % (W, H),
        "--window-position=-32000,-32000",
    ]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        hwnd = E.find_hwnd(proc.pid, timeout=30)
        if not hwnd:
            print("找不到窗口")
            return 2
        hwnds.append(hwnd)
        print("主窗口 hwnd=%x pid=%d" % (hwnd, proc.pid))
        print()
        print("=== 阶段 1：原样（离屏 + 从未 show 过），逐秒观察 ===")
        for i in range(6):
            row(hwnd, "t=%.1fs" % (i * 1.0))
            time.sleep(1.0)
        print()
        print("=== 阶段 2：SW_SHOWNA（显示但不激活）之后 ===")
        u.ShowWindow(hwnd, E.SW_SHOWNA)
        for i in range(4):
            time.sleep(0.4)
            row(hwnd, "show+%.1fs" % ((i + 1) * 0.4))
        print()
        print("=== 阶段 3：搬回屏幕内（+200+150）再看 ===")
        u.SetWindowPos(hwnd, 0, 200, 150, 0, 0,
                       0x0001 | 0x0004 | E.SWP_FRAMECHANGED)   # NOSIZE|NOZORDER
        for i in range(4):
            time.sleep(0.4)
            row(hwnd, "moveIn+%.1fs" % ((i + 1) * 0.4))
        print()
        print("=== 阶段 4：同一进程里所有 Chrome_WidgetWin 窗口 ===")
        for w in E.enum_windows(proc.pid):
            h = w["hwnd"]
            st = E._s32(u.GetWindowLongW(h, E.GWL_STYLE))
            pt = wintypes.POINT(0, 0)
            ok = bool(u.ClientToScreen(h, ctypes.byref(pt)))
            print("  hwnd=%x cls=%s 可见=%s rect=%s 面积分=%.0f style=%08x"
                  % (h, w["cls"], w["visible"], w["rect"],
                     E._score_window(w), st & 0xFFFFFFFF))
            print("        → 客户区内缩=%s   ClientToScreen=%s %s"
                  % (E.client_insets(h), ok, (pt.x, pt.y) if ok else "-"))
        print()
        print("=== 阶段 5：单独把「离屏极远」那个窗口搬进来再量 ===")
        for w in E.enum_windows(proc.pid):
            r = w["rect"]
            if r[0] < -30000 or r[1] < -30000:
                h = w["hwnd"]
                print("  离屏窗口 hwnd=%x rect=%s 内缩=%s" % (h, r, E.client_insets(h)))
                u.SetWindowPos(h, 0, 300, 200, 0, 0, 0x0001 | 0x0004)
                time.sleep(1.0)
                print("    搬进来之后 内缩=%s rect=%s"
                      % (E.client_insets(h), _rect_of(h)))
    finally:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

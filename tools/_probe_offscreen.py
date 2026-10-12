# -*- coding: utf-8 -*-
"""
为什么 attach() 量到的非客户区是 (0,0,0,0)？

现象（CLEANTV_DBGWIN=1 跑 check_wvt_embed.py 的日志）：
    候选 hwnd=820aec cls=Chrome_WidgetWin_1 可见=False
         rect=(-26214,-26214,-24934,-25494)  ← 1280x720，正是 --window-size=
         标题='tv.cctv.com/live/cctv13'      ← 这就是真窗口（页面都加载了）
    冻结内缩值=(0,0,0,0)

也就是说：**窗口挑对了，但它被放在极远处时就量不出非客户区**。
而 tools/_probe_frame2.py（正常位置启动）量到的是 (9,38,9,9)，且它在 -32000 时
也能量到 38 —— 差别可能在于 Chromium 把 -32000 重新折算成了 -26214，
跨过某个阈值就不画边框了。

本脚本用**完全相同的 ensure() 启动参数**，只换 --window-position，逐个验：
    A) -32000,-32000   （当前代码）
    B) -2000,-2000     （离屏但不太远）
    C) 0,0             （直接放在主屏左上角，靠隐身守卫压住）
每组都用全新 profile，量「刚找到窗口时」和「SW_SHOWNA 之后连续 2 秒」的内缩。
"""
import os
import sys
import time
import ctypes
import shutil
import tempfile
import subprocess

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_HERE, "desktop"))
sys.path.insert(0, _HERE)

import embed as E                                  # noqa: E402

u = ctypes.windll.user32
SIZE = (1280, 720)

BASE = [
    "--remote-debugging-port=%d",
    "--user-data-dir=",
    "--app=",
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
    "--window-size=%d,%d",
]


def launch(pos):
    import cdp
    exe = cdp.find_browser()
    udd = tempfile.mkdtemp(prefix="probeish_")
    port = 19600 + (abs(hash(pos)) % 200)
    url = "https://tv.cctv.com/live/cctv13/"
    args = [exe]
    for a in BASE:
        if a.endswith("port=%d"):
            args.append("--remote-debugging-port=%d" % port)
        elif a.endswith("user-data-dir="):
            args.append("--user-data-dir=" + udd)
        elif a.endswith("app="):
            args.append("--app=" + url)
        elif a.endswith("size=%d,%d"):
            args.append("--window-size=%d,%d" % SIZE)
        else:
            args.append(a)
    args.append("--window-position=" + pos)
    print("  位置参数 =", pos)
    p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=E.CREATE_NO_WINDOW)
    return p, udd


def style_str(hwnd):
    st = E._s32(u.GetWindowLongW(hwnd, E.GWL_STYLE))
    names = []
    for bit, nm in ((E.WS_POPUP, "POPUP"), (E.WS_CAPTION, "CAPTION"),
                    (E.WS_THICKFRAME, "THICK"), (E.WS_SYSMENU, "SYSMENU"),
                    (E.WS_CHILD, "CHILD"), (E.WS_MINIMIZE, "MIN")):
        if st & bit:
            names.append(nm)
    return "%08x [%s]" % (st & 0xFFFFFFFF, "|".join(names))


def run(pos):
    proc, udd = launch(pos)
    try:
        hwnd = None
        t0 = time.time()
        while time.time() - t0 < 12:
            ws = [w for w in E.enum_windows(proc.pid)
                  if (w["rect"][2] - w["rect"][0], w["rect"][3] - w["rect"][1]) == SIZE]
            if ws:
                ws.sort(key=lambda w: (bool(w["title"]), w["visible"]))
                hwnd = ws[-1]["hwnd"]
                break
            time.sleep(0.2)
        if not hwnd:
            print("  ★ 12s 内没找到尺寸正确的窗口")
            return
        print("  找到 hwnd=%x 可见=%s rect=%s" % (
            hwnd, bool(u.IsWindowVisible(hwnd)), E._rect_of(hwnd)
            if hasattr(E, "_rect_of") else None))
        print("     style(量之前) =", style_str(hwnd))
        print("     ① 直接量（可能已被算作不可见）:", E.client_insets(hwnd))
        u.ShowWindow(hwnd, E.SW_SHOWNA)
        for i in range(6):
            time.sleep(0.35)
            print("     ② SHOWNA +%.1fs 内缩=%s 可见=%s rect=%s" % (
                0.35 * (i + 1), E.client_insets(hwnd),
                bool(u.IsWindowVisible(hwnd)), E._rect_win(hwnd)))
        # 再搬到屏幕里试试
        u.MoveWindow(hwnd, 60, 60, SIZE[0], SIZE[1], True)
        time.sleep(0.6)
        print("     ③ 搬进屏幕(60,60) 内缩=%s 可见=%s" % (
            E.client_insets(hwnd), bool(u.IsWindowVisible(hwnd))))
    finally:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=8)
        except Exception:
            pass
        time.sleep(0.5)
        try:
            shutil.rmtree(udd, ignore_errors=True)
        except Exception:
            pass


def _rect_win(hwnd):
    r = E.wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


E._rect_win = _rect_win

if __name__ == "__main__":
    for tag, pos in (("A", "-32000,-32000"), ("B", "-2000,-2000"), ("C", "0,0")):
        print()
        print("========== 变体 %s ==========" % tag)
        run(pos)
    print()
    print("判据：哪一组能稳定量到 上边内缩>0（如 (9,38,9,9)），就用哪个位置参数。")

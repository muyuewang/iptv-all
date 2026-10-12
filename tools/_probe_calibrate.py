# -*- coding: utf-8 -*-
"""
客户区标定：Chromium 自绘的标题栏到底占了多少？

背景（tools/_probe_offscreen.py 三组共一次性的实测）：
    变体 A -32000,-32000   client_insets=(0,0,0,0)   ← 连搬到屏幕内也是 0
    变体 B -2000,-2000     client_insets=(0,0,0,0)
    变体 C 0,0             client_insets=(7,0,8,8)   ← 左右下有，顶上依然 0
    三组的 GWL_STYLE 都是 [CAPTION|THICKFRAME|SYSMENU]，但 Windows 报的非客户区
    顶边恒为 0 —— 那条 ~38px 的栏是 Chromium **画在客户区里**的，父窗口裁不掉。

所以别猜 38 这个数，直接**量**：
    把窗口摆成客户区高 H，用 CDP 读页面的 innerWidth/innerHeight（CSS px）。
    两者是线性关系：   H = k * innerHeight + B
    取两个不同的 H 解出 k（缩放系数）和 B（自绘 chrome 的高度，单位=MoveWindow 的单位）。
之后想让画面铺满宿主，就把窗口客户区做成 `k*目标高 + B`。

★ 为什么要两点：本机是 125% 缩放，CSS px 和 MoveWindow 用的逻辑 px 不是 1:1，
  单点只能解出一个未知数；两点才能同时解出 k 和 B。
"""
import os
import sys
import time
import json
import ctypes
import shutil
import tempfile
import subprocess

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_HERE, "desktop"))
sys.path.insert(0, _HERE)

import embed as E                                  # noqa: E402
import cdp                                         # noqa: E402

u = ctypes.windll.user32
URL = "https://tv.cctv.com/live/cctv13/"


def client_rect(hwnd):
    r = E.wintypes.RECT()
    u.GetClientRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def win_rect(hwnd):
    r = E.wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def main():
    exe = cdp.find_browser()
    if not exe:
        print("没找到 Edge/Chrome")
        return
    udd = tempfile.mkdtemp(prefix="probe_cal_")
    port = cdp.free_port(19700)
    args = [exe,
            "--remote-debugging-port=%d" % port,
            "--user-data-dir=" + udd,
            "--app=" + URL,
            "--no-first-run", "--no-default-browser-check", "--disable-sync",
            "--disable-breakpad", "--disable-component-update",
            "--no-service-autorun", "--password-store=basic",
            "--disable-popup-blocking", "--noerrdialogs",
            "--hide-crash-restore-bubble",
            "--autoplay-policy=no-user-gesture-required",
            "--disable-features=Translate,MediaRouter,CalculateNativeWinOcclusion",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-background-timer-throttling",
            "--disable-ipc-flooding-protection",
            "--window-size=1280,760",
            "--window-position=-32000,-32000"]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=E.CREATE_NO_WINDOW)
    print("pid =", proc.pid)
    try:
        hwnd = E.find_hwnd(proc.pid, timeout=25, expect=(1280, 760))
        if not hwnd:
            print("★ 没找到窗口")
            return
        ws = None
        t0 = time.time()
        while time.time() - t0 < 30:
            try:
                pages = cdp.wait_targets(port, timeout=2)
                if pages:
                    ws = cdp.MiniWS(pages[0]["webSocketDebuggerUrl"], timeout=40)
                    break
            except Exception:
                time.sleep(0.4)
        if ws is None:
            print("★ CDP 连不上")
            return
        ws.call("Page.enable", {})
        ws.call("Runtime.enable", {})

        def measure():
            cid = ws.send_call("Runtime.evaluate", {
                "expression": "JSON.stringify({w:innerWidth,h:innerHeight,"
                              "dpr:devicePixelRatio,oh:outerHeight})",
                "returnByValue": True})
            r = ws.wait_result(cid, timeout=15) or {}
            try:
                return json.loads(r["result"]["result"]["value"])
            except Exception:
                return None

        print()
        print("=== 两点标定（窗口还在离屏的顶层状态）===")
        rows = []
        for H in (700, 800):
            u.MoveWindow(hwnd, -32000, -32000, 1280, H, True)
            time.sleep(1.2)
            ins = E.client_insets(hwnd)
            u.ShowWindow(hwnd, E.SW_SHOWNA)
            time.sleep(0.8)
            ins = E.client_insets(hwnd)
            m = measure()
            wr, cr = win_rect(hwnd), client_rect(hwnd)
            print("  MoveWindow 高=%d → 窗口=%dx%d 客户区=%dx%d 内缩=%s  页面=%s"
                  % (H, wr[2] - wr[0], wr[3] - wr[1],
                     cr[2] - cr[0], cr[3] - cr[1], ins, m))
            if m:
                rows.append((H, cr[3] - cr[1], m["w"], m["h"], m["dpr"]))
            u.ShowWindow(hwnd, E.SW_HIDE)

        if len(rows) == 2:
            (h1, c1, _w1, i1, d1), (h2, c2, _w2, i2, d2) = rows
            print()
            if i2 != i1:
                k = (h2 - h1) / float(i2 - i1)
                B = h1 - k * i1
                print("  ★ 客户区高 = %.4f × innerHeight + %.2f" % (k, B))
                print("     （MoveWindow 单位 / CSS px = %.4f；自绘 chrome 高 = %.1f）"
                      % (k, B))
                print("     客户区实际高度分别是 %d / %d，dpr=%s"
                      % (c1, c2, d1))
                print("     → 想让画面占满 Hᶜ 的客户区，MoveWindow 高度应设为 %.1f" % (k * c1 + B))
            else:
                print("  ★ 两次 innerHeight 一样（%d），页面没有跟随窗口变化" % i1)
    finally:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=8)
        except Exception:
            pass
        time.sleep(0.4)
        shutil.rmtree(udd, ignore_errors=True)


if __name__ == "__main__":
    main()

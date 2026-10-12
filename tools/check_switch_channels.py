# -*- coding: utf-8 -*-
"""
check_switch_channels.py —— 复现用户反馈：「官网源快速切几个频道后，
浏览器跑出去自己在桌面上播（任务栏多出 浏览器+央视频），exe 里一片黑」。

流程（源码跑，逻辑与 exe 完全一致）：
  1. 启动 -> 官网源
  2. 挑 3~4 个**不同站点**的频道（tv.cctv.com / yangshipin.cn / 咪咕 MOBILE…）
     每隔 ~4 秒切一个，模拟用户连点
  3. 全程 5 次/秒扫描顶层 Chrome_WidgetWin_* 窗口：
     任何「可见、在屏幕内、不是我们嵌入的子窗口」的浏览器窗口 = 游离（FAIL）
  4. 收尾断言：
     - embed.hwnd 是 video_host 的子窗口
     - 全程游离曝光次数 = 0（隐藏守卫兜住的 ≤1 轮 20ms 不算）
     - cleantv.log 里没有「互杀风暴」（重启/放弃内嵌 各 ≤2 次）
"""
import os
import sys
import time
import ctypes
from ctypes import wintypes

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
import embed as _embed           # noqa: E402

_u32 = ctypes.windll.user32
FAILS = []
NOTES = []


def check(ok, msg):
    print("  [%s] %s" % ("OK" if ok else "!!", msg))
    if not ok:
        FAILS.append(msg)


def scan_visible_browsers(host_hwnd):
    """所有「可见 + 在屏幕内」的顶层 Chrome_WidgetWin_* 顶层窗口。"""
    out = []
    CBTYPE = wintypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, lp):
        cls = ctypes.create_unicode_buffer(256)
        _u32.GetClassNameW(hwnd, cls, 256)
        if "Chrome_WidgetWin" not in (cls.value or ""):
            return True
        if not _u32.IsWindowVisible(hwnd):
            return True
        if host_hwnd and _u32.IsChild(host_hwnd, hwnd):
            return True                      # 嵌进播放区的，不算游离
        r = wintypes.RECT()
        if not _u32.GetWindowRect(hwnd, ctypes.byref(r)):
            return True
        # 离屏（-32000）的不算露脸；在屏幕内或附近才算
        if r.right <= -20000 or r.bottom <= -20000:
            return True
        if r.right - r.left <= 1 or r.bottom - r.top <= 1:
            return True
        out.append((hwnd, (r.left, r.top, r.right, r.bottom)))
        return True

    _u32.EnumWindows(CBTYPE(cb), 0)
    return out


def main():
    app = na.NativeApp()
    host_hwnd = None
    stray_hits = []          # (t, hwnd, rect)
    monitor_on = [False]

    def monitor():
        while monitor_on[0]:
            try:
                for hwnd, rect in scan_visible_browsers(host_hwnd):
                    stray_hits.append((time.time(), hwnd, rect))
            except Exception:
                pass
            time.sleep(0.2)

    def step0():
        nonlocal host_hwnd
        # ★ 频道表同步预载：worker 线程里 root.after 有偶发竞态（与本测试无关），
        #   频道数据本来就是缓存秒读，直接在主线程灌进去。
        try:
            _wvt = na._wvt
            if _wvt is None:
                raise RuntimeError("wvt 模块不可用")
            chans = _wvt.load_channels(cache_dir=".")
            ads = _wvt.load_adapters(cache_dir=".")
            app._after_wvt_load(chans, ads)
        except Exception as e:
            print("预载失败:", e)
        host_hwnd = app.video_host.winfo_id()
        monitor_on[0] = True                 # 开游离窗口监视
        app.root.after(800, step_pick)

    def wait_wvt(n):
        app.root.update_idletasks()
        if n % 20 == 0:
            print("   [wait] n=%d wvt_loaded=%s channels=%d"
                  % (n, app.wvt_loaded, len(app.wvt_channels)))
        if not app.wvt_loaded and n < 400:
            app.root.after(100, wait_wvt(n + 1))
            return
        step_pick()

    def step_pick():
        chs = app.wvt_channels
        picks, seen_sites = [], set()
        # 优先挑「不同站点」的频道：cctv.com / yangshipin / 咪咕(MOBILE)…
        # ★ 咪咕/广东是 MOBILE UA —— 切到它们会触发「换 UA 重启浏览器」路径，
        #   这正是互杀风暴的高发口，必须覆盖。
        def site_of(c):
            u0 = (c.get("urls") or [""])[0]
            for s in ("yangshipin", "miguvideo", "gdtv", "cctv.com", "mgtv"):
                if s in u0:
                    return s
            return (u0.split("//")[-1].split("/")[0] if u0 else c["name"])

        priority = ["cctv.com", "yangshipin", "miguvideo", "gdtv"]
        for want in priority:
            for c in chs:
                if site_of(c) == want:
                    picks.append((c, want))
                    break
        seen_sites = {s for _c, s in picks}
        for c in chs:
            s = site_of(c)
            if s in seen_sites:
                continue
            seen_sites.add(s)
            picks.append((c, s))
            if len(picks) >= 5:
                break
        print("选中频道：" + "; ".join("%s(%s)" % (c["name"], s) for c, s in picks))
        if len(picks) < 2:
            check(False, "官网源频道太少，测不了切换")
            finish()
            return
        cycle(picks, 0)

    def cycle(picks, i):
        if i >= len(picks):
            app.root.after(20000, finish)     # 最后留 20s 观察稳态
            return
        c, s = picks[i]
        print(">> 切到 %s（%s） t=%.1f" % (c["name"], s, time.time() % 1000))
        app._open_channel(c.get("tid", "wvt"), c["id"], c["name"])
        app.root.after(4500, lambda: cycle(picks, i + 1))

    def finish():
        monitor_on[0] = False
        print("== 收尾断言 ==")
        emb = app.embed
        ok_child = False
        if emb is not None and emb.hwnd and _u32.IsWindow(emb.hwnd):
            ok_child = bool(_u32.IsChild(app.video_host.winfo_id(), emb.hwnd))
            vis = bool(_u32.IsWindowVisible(emb.hwnd))
            print("  embed.hwnd=%x IsChild=%s 可见=%s" % (emb.hwnd, ok_child, vis))
        check(ok_child, "最终频道确实内嵌在播放区里")
        check(len(stray_hits) == 0,
              "全程游离浏览器窗口曝光 0 次（实际 %d 次）" % len(stray_hits))
        for t, hwnd, rect in stray_hits[:5]:
            print("    游离 hwnd=%x rect=%s" % (hwnd, rect))
        # 日志风暴检查
        storm = 0
        try:
            with open("cleantv.log", "r", encoding="utf-8") as f:
                lines = f.readlines()[-400:]
            storm = sum(1 for L in lines if ("重启一次浏览器" in L or "放弃内嵌" in L))
        except Exception:
            pass
        check(storm <= 2, "无互杀风暴（重启/放弃内嵌 %d 次）" % storm)
        n = len(getattr(emb, "_spawned", []) or []) if emb else 0
        print("  本轮共起过 %d 只浏览器进程" % n)
        print("RESULT", "FAIL" if FAILS else "PASS")
        app.root.after(100, app.root.destroy)
        NOTES.append("done")

    th = None

    def start_all():
        app.root.after(1500, step0)
        th = __import__("threading").Thread(target=monitor, daemon=True)
        th.start()

    app.root.after(300, start_all)
    app.root.mainloop()


if __name__ == "__main__":
    main()
    print("\n".join(NOTES))

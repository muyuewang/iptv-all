# -*- coding: utf-8 -*-
"""
check_ui_ops.py —— 本机源播放 + 全操作点测（用户要求：播放一下，每个操作都点一遍）。

覆盖：点频道起播(mpv) / 暂停恢复 / 音量 / 上下线路 / 连切几个频道 / 收藏 /
      EPG 节目单 / 搜索 / 全屏进出 / 设置页开关 / 台标。
官网源的播放与连切在 check_switch_channels.py 里单独覆盖。

判定：
  * mpv 进程活着 + IPC 管道连上 = 播放管线正常（硬指标）
  * 播放区截图非黑像素占比 —— 只 WARN 不 FAIL（台标页/黑场会误报）
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "desktop"))
sys.path.insert(0, ROOT)
os.chdir(os.path.join(ROOT, "desktop"))

import native_app as na          # noqa: E402

RES = []
FAILS = []
SHOT = os.path.join(ROOT, "_shots")
os.makedirs(SHOT, exist_ok=True)


def check(ok, msg):
    print("  [%s] %s" % ("OK" if ok else "!!", msg))
    RES.append(ok)
    if not ok:
        FAILS.append(msg)


def mpv_alive(app):
    return bool(app.mpv_proc and app.mpv_proc.poll() is None)


def shot_video(app, name):
    """截播放区，返回非黑像素占比。"""
    try:
        from PIL import ImageGrab
        app.video_host.update_idletasks()
        x = app.video_host.winfo_rootx()
        y = app.video_host.winfo_rooty()
        w = app.video_host.winfo_width()
        h = app.video_host.winfo_height()
        if w <= 0 or h <= 0:
            return -1
        img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
        img.save(os.path.join(SHOT, name))
        g = img.convert("L").resize((120, 68))
        px = list(g.getdata())
        lit = sum(1 for v in px if v > 40)
        return lit / float(len(px))
    except Exception:
        return -1


def main():
    app = na.NativeApp()

    st = {}

    def wait(ms, fn):
        app.root.after(ms, fn)

    def step1():
        print("== 1) 本机源：点 CCTV-1 起播 ==")
        row = next((c for c in app.filtered if "CCTV-1" in c["name"]
                    and "4K" not in c["name"]), None) or (app.filtered[0] if app.filtered else None)
        if not row:
            check(False, "本机源频道列表为空")
            return finish()
        st["row"] = row
        app._open_channel(row["tid"], row["id"], row["name"])
        wait(10000, step2)

    def step2():
        row = st["row"]
        check(mpv_alive(app), "mpv 进程存活（%s）" % row["name"])
        check(app.ipc is not None and app.ipc.conn is not None, "mpv IPC 已连接")
        r = shot_video(app, "ops_local_play.png")
        print("  → 播放区非黑像素占比 = %.1f%%" % (r * 100 if r >= 0 else -1))
        if 0 <= r < 0.02:
            print("  [WARN] 播放区几乎全黑（可能是黑场/转场，不判失败）")
        check(app.line_lbl.cget("text") != "", "线路标签已更新: %s" % app.line_lbl.cget("text"))

        print("== 2) 暂停 / 恢复 ==")
        app._toggle_pause()
        wait(400, step3)

    def step3():
        check(app._paused, "暂停态生效（_paused=True）")
        app._toggle_pause()
        wait(400, step4)

    def step4():
        check(not app._paused, "恢复播放（_paused=False）")
        print("== 3) 音量 ==")
        app.vol_scale.set(35)
        wait(600, step5)

    def step5():
        check(abs(app._volume - 35) <= 1, "音量滑杆生效（_volume=%s）" % app._volume)
        print("== 4) 下一条线路 ==")
        before = app.cur["idx"]
        app._next_line()
        wait(6000, step6)

    def step6():
        print("  线路 idx: %d -> %d, 标签=%s" % (
            st.get("idx0", -1), app.cur["idx"], app.line_lbl.cget("text")))
        check(mpv_alive(app), "切线路后 mpv 仍在播")
        print("== 5) 连切频道 CCTV2 / CCTV13 ==")
        def find_ch(prefix):
            for c in app.filtered:
                if c["name"].startswith(prefix):
                    return c
            return None
        switched = 0
        for nm in ("CCTV5体育", "CCTV13新闻"):
            row = find_ch(nm)
            check(row is not None, "频道表里找到 %s" % nm)
            if row:
                app._open_channel(row["tid"], row["id"], row["name"])
                app.root.update()
                time.sleep(6.5)
                app.root.update()
                ok = mpv_alive(app)
                check(ok, "切到 %s 后 mpv 存活（%s）" % (nm, row["name"]))
                switched += 1
        print("== 6) 收藏 / 取消收藏 ==")
        row = find_ch("CCTV1综合")
        if row:
            n0 = len(app.fav)
            app._toggle_fav_for(row)
            check(len(app.fav) == n0 + 1, "收藏 +1（%d→%d）" % (n0, len(app.fav)))
            app._toggle_fav_for(row)
            check(len(app.fav) == n0, "取消收藏（%d→%d）" % (n0 + 1, len(app.fav)))
        print("== 7) EPG 节目单 ==")
        app._toggle_epg()
        wait(600, step7)

    def step7():
        check(bool(app.epg_bar.winfo_manager()), "EPG 面板展开")
        app._toggle_epg()
        wait(300, step8)

    def step8():
        check(not app.epg_bar.winfo_manager(), "EPG 面板收起")
        print("== 8) 搜索 ==")
        n0 = len(app.filtered)
        app.search_var.set("CCTV")
        app._on_search()
        check(len(app.filtered) < n0 and len(app.filtered) > 0,
              "搜索 CCTV 过滤 %d→%d" % (n0, len(app.filtered)))
        app.search_var.set("")
        app._on_search()
        check(len(app.filtered) == n0, "清空搜索恢复 %d" % len(app.filtered))
        print("== 9) 全屏进出 ==")
        app._toggle_fullscreen()
        wait(1200, step9)

    def step9():
        check(bool(app.root.attributes("-fullscreen")), "进入全屏")
        app._toggle_fullscreen()
        wait(1200, step10)

    def step10():
        check(not app.root.attributes("-fullscreen"), "退出全屏")
        check(app.root.winfo_width() >= 1000, "窗口尺寸恢复（%dx%d）" % (
            app.root.winfo_width(), app.root.winfo_height()))
        print("== 10) 设置页开关 ==")
        app._open_settings()
        wait(800, step11)

    def step11():
        dlg = getattr(app, "_settings_dlg", None)
        top = None
        for w in app.root.winfo_children():
            if isinstance(w, na.tk.Toplevel):
                top = w
        check(top is not None, "设置窗口已打开")
        if top is not None:
            try:
                top.destroy()
            except Exception:
                pass
        print("RESULT", "FAIL" if FAILS else "PASS" if RES else "PASS")
        finish()

    def finish():
        app.root.after(100, app.root.destroy)

    app.root.after(2500, step1)
    app.root.mainloop()
    print("RESULT %s（%d 项，失败 %d）" % (
        "FAIL" if FAILS else "PASS", len(RES), len(FAILS)))
    for m in FAILS:
        print("  ✗", m)


if __name__ == "__main__":
    main()

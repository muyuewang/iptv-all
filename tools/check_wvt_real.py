# -*- coding: utf-8 -*-
"""官网源「真嗅探」端到端验证：走 native_app 的完整路径，只把最后的 mpv 换掉。

用法：python tools/check_wvt_real.py "CCTV-1 综合"
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "desktop"))

import native_app as N  # noqa: E402

TARGET = sys.argv[1] if len(sys.argv) > 1 else "CCTV-1 综合"
T0 = time.time()

print("== 启动 ==")
app = N.NativeApp()
if N._cdp is None:
    print("cdp 不可用，退出")
    sys.exit(3)

played = []
app._start_mpv = lambda url, headers=None: played.append((url, headers))
app._open_web_window = lambda url: played.append(("BROWSER:" + url, None))

STATE = {}


def finish(code):
    print()
    print("总用时 %.1fs" % (time.time() - T0))
    try:
        N._cdp.close_default_session()
    except Exception:
        pass
    try:
        app.root.destroy()
    except Exception:
        pass
    sys.exit(code)


def step1():
    print("== 载入官网源 ==")
    app._set_src("wvt")
    poll(0)


def poll(n):
    if app.wvt_loaded:
        print("  频道 %d / 分组 %d" % (len(app.wvt_channels), len(app.wvt_groups)))
        step2()
    elif n > 600:
        print("  加载超时")
        finish(2)
    else:
        app.root.after(100, lambda: poll(n + 1))


def step2():
    print("== 找目标频道：%s ==" % TARGET)
    cands = [c for c in app.wvt_channels if c["name"] == TARGET]
    if not cands:
        cands = [c for c in app.wvt_channels if TARGET in c["name"]]
    if not cands:
        print("  没找到")
        finish(2)
    c = cands[0]
    STATE["c"] = c
    print("  %s / %s / %d 条线路" % (c["group"], c["name"], len(c["urls"])))
    for i, u in enumerate(c["urls"][:3]):
        print("    线路%d: %s" % (i + 1, u[:100]))
    app._load_group(c["group"])
    app.root.after(300, step3)


def step3():
    print("== 点击播放（等待真实嗅探）==")
    c = STATE["c"]
    app._open_channel(c["tid"], c["id"], c["name"])
    poll_play(0)


def poll_play(n):
    if played:
        return done()
    if n % 40 == 0 and n:
        print("  ... %.0fs  %s" % (time.time() - T0, app.status_lbl.cget("text")))
    if n > 1500:                      # 150s
        print("  超时")
        return done(2)
    app.root.after(100, lambda: poll_play(n + 1))


def done(code=None):
    print()
    if played:
        url, hdrs = played[-1]
        print("结果: %s" % url[:160])
        hf = app._header_fields(hdrs) if hdrs else ""
        print("请求头: %s" % (hf[:200] or "(无)"))
        if not url.startswith("BROWSER:") and ".m3u8" in url:
            finish(0)
        else:
            finish(1)
    else:
        print("结果: 未拿到任何播放地址")
        finish(code or 2)


app.root.after(300, step1)
app.root.mainloop()

# -*- coding: utf-8 -*-
"""诊断：换台到底黑多久（亮度时间线）。

为什么需要
----------
「无缝换台」这类需求最容易被主观判断糊过去 —— 自己盯着看总觉得"没那么黑"。
这个探针把整屏平均亮度按 ~0.28s 采一串，直接给出「最长连续暗屏秒数」，
改前改后各跑一次就有硬数字。

两种换台路径分开测（它们的实现完全不同）：

    python _probe_seam.py inplay    # A) 播放层内按「频道+」直接切（player.html:1882）
    python _probe_seam.py back      # B) BACK 回列表 → 点另一个频道

依赖：tools/tvlab.py
"""
import sys
import time

from tvlab import (arm_native_probe, banner, boot_app, bright_timeline, cap,
                   key, open_local, page_state, pick_channel, sh, tracks,
                   Verdict, wait_ready, PKG)


def wait_list(cdp, timeout=25):
    t0 = time.time()
    while time.time() - t0 < timeout:
        n = cdp.eval_js("document.querySelectorAll('#list .ch').length", 8)
        try:
            if int(n) > 0:
                return int(n)
        except Exception:
            pass
        time.sleep(1.5)
    return 0


def inplay(cdp, v):
    banner("A) 播放层内「频道+」切台 —— 画面应停在上一帧")
    arm_native_probe(cdp)
    print("open(广东):", pick_channel(cdp, "广东"), flush=True)
    ok = wait_ready(cdp)
    print("起播:", ok, page_state(cdp), flush=True)
    v.check(ok, "首个频道已起播")

    time.sleep(1.5)
    base, w0 = bright_timeline(sec=4.0, label="基线")
    v.check(w0 < 1.0, "基线期间画面明亮（最长暗屏 %.2fs）" % w0)

    print("→ 发 KEYCODE_CHANNEL_UP(166)", flush=True)
    key(166)
    _, worst = bright_timeline(sec=16.0, label="切台中")
    st = page_state(cdp)
    n, a, _ = tracks()
    cap("seam_inplay.png")
    print("切台后:", st, "audio=%d/%d" % (n, a), flush=True)
    v.check("show" in (st.get("pcls") or ""), "切台后仍在播放层 (%s)" % st.get("pcls"))
    v.check(worst <= 1.2, "切台过程无长黑屏（最长暗屏 %.2fs，希望 ≤1.2s）" % worst)


def back(cdp, v):
    banner("B) BACK 回列表 → 再点频道")
    arm_native_probe(cdp)
    print("open(广东):", pick_channel(cdp, "广东"), flush=True)
    ok = wait_ready(cdp)
    v.check(ok, "首个频道已起播")
    time.sleep(1.5)

    _, w0 = bright_timeline(sec=3.0, label="基线")
    v.check(w0 < 1.0, "基线期间画面明亮（最长暗屏 %.2fs）" % w0)

    print("→ BACK", flush=True)
    key("BACK")
    _, wb = bright_timeline(sec=4.0, label="回列表")
    v.check(wb < 1.2, "回列表过程无长黑屏（最长暗屏 %.2fs）" % wb)

    st = page_state(cdp)
    print("BACK 后:", st, flush=True)
    v.check(st.get("frozen") is True, "画面层已保帧（frozen=%s）" % st.get("frozen"))
    v.check("show" not in (st.get("pcls") or ""), "播放层已关闭 (pcls=%s)" % st.get("pcls"))

    n = wait_list(cdp)
    print("list:", n, flush=True)
    print("open(广东):", pick_channel(cdp, "广东"), flush=True)
    _, wc = bright_timeline(sec=14.0, label="重开同台")
    st = page_state(cdp)
    cap("seam_back.png")
    print("新台:", st, flush=True)
    v.check("show" in (st.get("pcls") or ""), "新台仍在播放层 (%s)" % st.get("pcls"))
    v.check(wc <= 1.0, "重开同台无长黑屏（最长暗屏 %.2fs，希望 ≤1.0s）" % wc)


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "inplay").strip()
    banner("换台黑屏探针 · %s" % mode)
    cdp = boot_app()
    if not cdp:
        print("!! 连不上 devtools", flush=True)
        return 2
    v = Verdict()
    open_local(cdp)
    if wait_list(cdp) == 0:
        print("!! 列表为空（接口偶发）", flush=True)
        return 3

    if mode == "inplay":
        inplay(cdp, v)
    elif mode == "back":
        back(cdp, v)
    else:
        print("用法: _probe_seam.py [inplay|back]", flush=True)
        return 2

    rc = v.done()
    sh(["shell", "am", "force-stop", PKG])
    print("=== DONE ===", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())

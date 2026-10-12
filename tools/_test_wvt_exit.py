# -*- coding: utf-8 -*-
"""回归：官网源（官网页 WebView 线路）起播 → 一次 BACK 干净回列表 + 无残留声音。

这一条同时覆盖 v2.4.8 的第三个根因：多线路频道自动切了几轮后 `webState` 停在 2
（「正在自动切线路」），用户按 BACK 被当成内部动作 → 只关官方页、播放层留着，
再按一次就直接退出 App。修法是原生 BACK 前先置 `window.__userExit=true`。

依赖：tools/tvlab.py
"""
import sys
import time

from tvlab import (banner, boot_app, cap, key, open_wvt, pick_channel, sh,
                   silence_check, tracks, Verdict, PKG)

CH = "CCTV-1"


def main():
    banner("官网源：起播 → BACK 一次退到列表 + 音频归零")
    cdp = boot_app()
    if not cdp:
        print("!! 连不上 devtools", flush=True)
        return 2
    v = Verdict()

    open_wvt(cdp)
    print("wvt open:", pick_channel(cdp, CH), flush=True)

    banner("观察 24 秒（期间可能发生自动切线路）")
    started = False
    for t in range(6):
        time.sleep(4)
        st = cdp.json("(function(){return JSON.stringify({"
                      "status:document.getElementById('status').textContent,"
                      "statusDisp:getComputedStyle(document.getElementById('status')).display,"
                      "mask:document.getElementById('webmask').className,"
                      "webState:webState,"
                      "pcls:document.getElementById('player').className});})()")
        n, a, _ = tracks()
        print("  [%02d] %s track=%d/%d" % (t, st, n, a), flush=True)
        if st and st.get("pcls", "").find("show") >= 0 and (a > 0 or n > 0):
            started = True
    cap("w1_wvt_playing.png")
    v.check(started, "官网页线路已起播（出现音频轨）")

    banner("按 BACK")
    key("BACK")
    time.sleep(4)
    st = cdp.json("(function(){return JSON.stringify({"
                  "pcls:document.getElementById('player').className,"
                  "webState:(typeof webState==='number'?webState:'?'),"
                  "mask:document.getElementById('webmask').className,"
                  "listN:document.querySelectorAll('#list .ch').length});})()")
    print("after BACK:", st, flush=True)
    cap("w2_after_back.png")
    v.check(bool(st) and "show" not in (st.get("pcls") or ""), "一次 BACK 即关闭播放层 (pcls=%s)" %
            (st or {}).get("pcls"))
    v.check(bool(st) and (st.get("mask") or "") == "", "网页遮罩已收起")
    v.check(bool(st) and (st.get("listN") or 0) > 0, "已回到频道列表 (listN=%s)" % (st or {}).get("listN"))

    v.check(silence_check(cdp, rounds=8, gap=4.0, label="W"), "退出后 32 秒全程音频归零")

    rc = v.done()
    sh(["shell", "am", "force-stop", PKG])
    print("=== DONE ===", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())

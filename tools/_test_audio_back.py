# -*- coding: utf-8 -*-
"""回归：BACK（退出播放层）后不得有音频残留。

这一条比 HOME 更容易漏 —— 按 BACK 时 Activity 还在前台，生命周期 onPause
不会触发，只有 `closePlayer` 里显式调 `NativeTV.pauseWebMedia()`（web.onPause）
才停得掉 Chromium 的 MSE 音频。同时退出时要摘掉原生画面层（TextureView）。

流程：本机源起播（确认真在出声）→ 按 BACK → 48 秒采样，全程零音频才算 PASS。
依赖：tools/tvlab.py
"""
import sys

from tvlab import (banner, boot_app, key, open_local, pick_channel, sh,
                   silence_check, tracks, Verdict, wait_playing, PKG)

CH = "广东"


def main():
    banner("BACK 场景：退出播放层后不得有音频残留")
    cdp = boot_app()
    if not cdp:
        print("!! 连不上 devtools", flush=True)
        return 2

    open_local(cdp)
    banner("打开 %s 频道并等起播" % CH)
    print("open:", pick_channel(cdp, CH), flush=True)

    if not wait_playing(cdp, tries=20):
        print("=== ABORT：始终没检测到起播 ===", flush=True)
        return 3

    n, a, rows = tracks()
    banner("S1 播放中 started=%d flinger_active=%d" % (n, a))
    for rr in rows:
        print("   ", rr, flush=True)

    key("BACK")
    print(">>> 已按 BACK", flush=True)

    v = Verdict()
    v.check(silence_check(cdp, rounds=12, gap=4.0, label="B"),
            "BACK 后 48 秒全程无 started 音频轨、flinger active 归零")
    # 顺带确认退出后确实回到了列表（播放层摘掉、列表可见）
    st = cdp.json("(function(){return JSON.stringify({"
                  "pcls:document.getElementById('player').className,"
                  "listN:document.querySelectorAll('#list .ch').length});})()")
    v.check(bool(st) and "show" not in (st.get("pcls") or ""), "播放层已关闭 (pcls=%s)" %
            (st or {}).get("pcls"))
    v.check(bool(st) and (st.get("listN") or 0) > 0, "频道列表已恢复 (listN=%s)" %
            (st or {}).get("listN"))

    rc = v.done()
    sh(["shell", "am", "force-stop", PKG])
    print("=== DONE ===", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())

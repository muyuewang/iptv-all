# -*- coding: utf-8 -*-
"""回归：HOME（回桌面）后不得有音频残留。

背景：v2.4.8 定案的双根因 —— 闸门作用域分裂（`__sessionClosed` 误声明在 init
闭包里，全局函数读的是另一个变量）+ `nativeStop` 跑在 JavaBridge 线程导致
media3 抛「wrong thread」而 ExoPlayer 实例泄漏。两者都在本脚本的判定下暴露过。

流程：本机源起播（确认真的在出声）→ 按 HOME → 48 秒连续采样 dumpsys，全程
      started=0 且 flinger_active=0 才算 PASS。
依赖：tools/tvlab.py
"""
import sys
import time

from tvlab import (banner, boot_app, key, open_local, pick_channel, sh,
                   silence_check, tracks, Verdict, wait_playing, PKG)

CH = "广东"


def main():
    banner("HOME 场景：后台不得有音频残留")
    cdp = boot_app()
    if not cdp:
        print("!! 连不上 devtools", flush=True)
        return 2

    open_local(cdp)
    banner("打开 %s 频道并等起播" % CH)
    print("open:", pick_channel(cdp, CH), flush=True)

    if not wait_playing(cdp):
        print("=== ABORT：始终没检测到起播 ===", flush=True)
        return 3

    n, a, rows = tracks()
    banner("S1 播放中 started=%d flinger_active=%d" % (n, a))
    for rr in rows:
        print("   ", rr, flush=True)

    key("HOME")
    print(">>> 已按 HOME", flush=True)

    v = Verdict()
    ok = silence_check(cdp, rounds=12, gap=4.0, label="H")
    v.check(ok, "HOME 后 48 秒全程无 started 音频轨、flinger active 归零")

    rc = v.done()
    sh(["shell", "am", "force-stop", PKG])
    print("=== DONE ===", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())

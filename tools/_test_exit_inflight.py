# -*- coding: utf-8 -*-
"""回归：请求在途时退出播放层 —— 异步回调不得把频道重新拉起来。

背景（v2.5.2 修复）
------------------
`openChannel()` 里 `C.fetchPlay()` 是异步 Promise，之后还有一轮并发测速
（`pickBestLine`）。**原来这两个回调都没有查会话闸门**，于是：

  · 用户在 token 请求返回前按 BACK → `.then` 仍执行 `playIdx()` →
    `resumeWebMedia()` + 起播 → **退回列表之后声音自己冒出来**（v2.4.7
    那个「返回后还有声音」的变体）；
  · 或者 `.catch` 把「网络连接异常」失败卡弹到已经退回的列表上。

修法：`.then` / `.catch` / 内层测速回调 各查一次 `__sessionClosed`，
并在 `playIdx()` 顶部加一道兜底闸门（它是所有起播异步链的汇聚点）。

本脚本判据
----------
点开频道后**立刻**按 BACK（故意抢在请求返回之前），再观察 20 秒：
播放层必须保持关闭、没有失败卡、没有音频、列表正常。

依赖：tools/tvlab.py
"""
import sys
import time

from tvlab import (banner, boot_app, cap, key, open_local, page_state,
                   pick_channel, sh, tracks, Verdict, PKG)

CH = "广东"


def main():
    banner("请求在途时退出播放层：不得自行复活")
    cdp = boot_app()
    if not cdp:
        print("!! 连不上 devtools", flush=True)
        return 2
    v = Verdict()

    open_local(cdp)
    st = page_state(cdp)
    print("列表:", st.get("listN"), flush=True)
    v.check((st.get("listN") or 0) > 0, "列表已就绪 (listN=%s)" % st.get("listN"))

    # 点开 → 不等待，立刻 BACK（刻意抢在 fetchPlay/测速返回之前）
    print("open:", pick_channel(cdp, CH), flush=True)
    time.sleep(0.25)
    key("BACK")
    print(">>> 点开后 0.25s 即按 BACK（此刻请求多半还在途）", flush=True)

    banner("观察 20 秒")
    bad = []
    for t in range(10):
        time.sleep(2)
        st = page_state(cdp)
        n, a, rows = tracks()
        print("  [%02d] pcls=%-8s card=%-6s toasts=%s natActive=%s | audio=%d/%d"
              % (t, st.get("pcls"), st.get("card"), st.get("toasts"),
                 st.get("natActive"), n, a), flush=True)
        if "show" in (st.get("pcls") or ""):
            bad.append("播放层自己又开了 (t=%ds)" % (t * 2))
        if st.get("card", "").find("show") >= 0:
            bad.append("失败卡弹到了列表上 (t=%ds)" % (t * 2))
        if n != 0 or a != 0:
            bad.append("有音频 (%d/%d @t=%ds)" % (n, a, t * 2))
    cap("exit_inflight.png")

    v.check(not any("播放层" in b for b in bad), "播放层全程保持关闭")
    v.check(not any("失败卡" in b for b in bad), "没有把失败卡弹到列表上")
    v.check(not any("音频" in b for b in bad), "全程无音频（未擅自复活播放）")
    st = page_state(cdp)
    v.check((st.get("listN") or 0) > 0, "列表仍正常 (listN=%s)" % st.get("listN"))
    for b in bad[:6]:
        print("   !!", b, flush=True)

    rc = v.done()
    sh(["shell", "am", "force-stop", PKG])
    print("=== DONE ===", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())

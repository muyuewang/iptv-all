# -*- coding: utf-8 -*-
"""回归：横竖屏 + 返回键三态（v2.4.8 的三个根因都在这里兜住）。

用户原话：「点频道自动转横屏播放、返回的时候竖屏状态看着很异常、再按返回直接
退出软件」。当时的三根因：
  ① release() 只释放 player，TextureView 留在宿主里显示最后一帧（WebView 透明）；
  ② closePlayer 用 web.onPause() 停 MSE 音频时连渲染一起停，露出下层残留帧；
  ③ 官网源 webState 停在 2（自动切线路）时，用户按 BACK 被当成内部动作。

本脚本按「横屏播放 → BACK → 再 BACK」走一遍，每步 dump 视口与页面状态并截图。

★ 旋转验证看 innerWidth/innerHeight 与 screen.orientation，别看 outerWidth ——
  它在旋转后一段时间仍是旧值，会误导「没转过去」的判断。

依赖：tools/tvlab.py
"""
import sys
import time

from tvlab import (banner, boot_app, cap, foreground, key, open_local,
                   page_state, pick_channel, sh, Verdict, wait_playing, PKG)

CH = "广东"


def main():
    banner("横竖屏 + 返回键回归")
    cdp = boot_app()
    if not cdp:
        print("!! 连不上 devtools", flush=True)
        return 2
    v = Verdict()

    banner("S0 启动后（竖屏列表）")
    st0 = page_state(cdp)
    print("S0:", st0, flush=True)
    cap("s0_list.png")
    v.check(st0.get("ih", 0) > st0.get("iw", 0), "S0 为竖屏 (iw=%s ih=%s)" % (st0.get("iw"), st0.get("ih")))
    v.check((st0.get("listN") or 0) > 0, "S0 频道列表已渲染 (listN=%s)" % st0.get("listN"))

    open_local(cdp)
    banner("打开 %s 并等起播" % CH)
    print("open:", pick_channel(cdp, CH), flush=True)
    v.check(wait_playing(cdp), "频道已起播")

    banner("S1 播放中（应为横屏）")
    st1 = page_state(cdp)
    print("S1:", st1, flush=True)
    cap("s1_playing.png")
    v.check(st1.get("iw", 0) > st1.get("ih", 0), "S1 已转横屏 (iw=%s ih=%s)" % (st1.get("iw"), st1.get("ih")))
    v.check("show" in (st1.get("pcls") or ""), "S1 播放层已显示 (pcls=%s)" % st1.get("pcls"))

    banner("S2 按 BACK（第一次）→ 应回竖屏列表")
    key("BACK")
    time.sleep(4)
    st2 = page_state(cdp)
    print("S2:", st2, flush=True)
    cap("s2_after_back.png")
    v.check(st2.get("ih", 0) > st2.get("iw", 0), "S2 已回竖屏 (iw=%s ih=%s)" % (st2.get("iw"), st2.get("ih")))
    v.check("show" not in (st2.get("pcls") or ""), "S2 播放层已关闭 (pcls=%s)" % st2.get("pcls"))
    v.check((st2.get("listN") or 0) > 0, "S2 频道列表可见 (listN=%s)" % st2.get("listN"))

    banner("S3 再按 BACK（第二次）→ 应退出到桌面")
    key("BACK")
    time.sleep(4)
    cap("s3_after_back2.png")
    fg = foreground()
    print("前台 Activity:", fg, flush=True)
    v.check("com.clean.tv" not in (fg or ""), "已退出 App（前台=%s）" % fg)

    rc = v.done()
    sh(["shell", "am", "force-stop", PKG])
    print("=== DONE ===", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())

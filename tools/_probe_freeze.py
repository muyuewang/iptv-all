# -*- coding: utf-8 -*-
"""诊断：TextureView 上的最后一帧在 ExoPlayer.release() 之后还在不在。

背景：无缝换台的思路是「BACK 时不摘画面层，让最后一帧留着」。但 release()
会不会顺手把纹理内容也清掉？这个脚本单独验它 —— 不猜。

做法：播广东 → 等起播 → 记亮度 → BACK → 确认 frozen → 手工把 WebView 转透明
（revealFrozenFrame）+ 藏掉列表 → 量亮度。
    亮度回到视频级（>40）→ 帧还在，保帧方案成立；
    亮度掉到 0~9        → 帧已经被清，需要换方案（Bitmap 抓帧或 pause 不 release）。

依赖：tools/tvlab.py
"""
import sys
import time

from tvlab import (arm_native_probe, banner, boot_app, cap, frame_mean, key,
                   open_local, page_state, pick_channel, sh, wait_ready, PKG)


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


def main():
    banner("诊断：release() 之后 TextureView 的最后一帧是否还在")
    cdp = boot_app()
    if not cdp:
        print("!! 连不上 devtools", flush=True)
        return 2
    open_local(cdp)
    if wait_list(cdp) == 0:
        print("!! 列表为空", flush=True)
        return 3
    arm_native_probe(cdp)

    print("open(广东):", pick_channel(cdp, "广东"), flush=True)
    print("起播:", wait_ready(cdp), flush=True)
    time.sleep(2.0)
    m_play = frame_mean()
    print("播放中亮度: %.1f" % m_play, flush=True)

    print("→ BACK", flush=True)
    key("BACK")
    time.sleep(2.0)
    st = page_state(cdp)
    print("BACK 后:", st, flush=True)
    m_list = frame_mean()
    print("列表亮度: %.1f（应远低于播放亮度）" % m_list, flush=True)

    print("→ 手工 setFrameOn()（把 WebView 转透明，藏掉列表/顶栏）", flush=True)
    print("  js:", cdp.eval_js("(setFrameOn(),1)"), flush=True)
    time.sleep(1.2)
    st2 = page_state(cdp)
    print("揭帧后:", st2, flush=True)
    m_froz = frame_mean()
    cap("freeze_probe.png")
    print("揭帧后亮度: %.1f" % m_froz, flush=True)

    if m_froz > 40:
        print("=> 结论：帧还在，保帧方案成立（frozen 值 %.1f）" % m_froz, flush=True)
    elif m_froz < 12:
        print("=> 结论：帧已被清空（亮度 %.1f），保帧方案需要换实现" % m_froz, flush=True)
    else:
        print("=> 结论：中间态（亮度 %.1f），需要人眼看截图 freeze_probe.png" % m_froz, flush=True)

    sh(["shell", "am", "force-stop", PKG])
    print("=== DONE ===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

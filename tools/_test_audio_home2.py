# -*- coding: utf-8 -*-
"""诊断（不是判定）：HOME 之后页面闸门与音频轨的归属状态。

用途：当 `_test_audio_min.py` 报 FAIL 时，用它区分「JS 闸门没落」还是
「OS 层音频轨没释放」—— 两者的修法完全不同（前者改 player.html，后者改 Java）。
判据看这三个值：
  closed=True   → `__sessionClosed` 闸门落下（挡自动重连）
  natActive=0   → 原生通道已释放
  player=null + video 已清空 → 网页侧清理到位
若这三项都正常而 started 仍 >0，问题一定在 Java 侧（ExoPlayer 实例泄漏）。

依赖：tools/tvlab.py
"""
import sys
import time

from tvlab import (banner, boot_app, key, open_local, pick_channel, sh, tracks,
                   Verdict, wait_playing, PKG)

CH = "广东"


def main():
    banner("HOME 后深挖：闸门状态 + 音频轨归属")
    cdp = boot_app()
    if not cdp:
        print("!! 连不上 devtools", flush=True)
        return 2

    open_local(cdp)
    print("open:", pick_channel(cdp, CH), flush=True)
    if not wait_playing(cdp):
        print("=== ABORT：没起播 ===", flush=True)
        return 3
    print(">>> 已确认真在出声", flush=True)

    key("HOME")
    print(">>> 已按 HOME", flush=True)
    time.sleep(8)

    banner("页面闸门状态（HOME 后 CDP 仍可连）")
    st = cdp.json("(function(){var o={};"
                  "o.pauseCount=window.__appPauseCount||0;"
                  "o.closed=(typeof __sessionClosed!=='undefined')?__sessionClosed:'undef';"
                  "o.natActive=(typeof NATIVE!=='undefined')?NATIVE.active:'undef';"
                  "o.player=((typeof player==='undefined')?'undef':(player===null?'null':typeof player));"
                  "o.wasPlaying=(typeof __appWasPlaying!=='undefined')?__appWasPlaying:'undef';"
                  "o.tune=(typeof TUNE!=='undefined')?TUNE:'undef';"
                  "var v=document.getElementById('v');"
                  "o.vsrc=(v&&v.src)?('HAS:'+v.src.slice(0,40)):'empty';"
                  "o.vct=v?v.currentTime:-1;"
                  "o.vpaused=v?v.paused:-1;"
                  "return JSON.stringify(o);})()")
    print("JS:", st, flush=True)

    v = Verdict()
    if st:
        v.check(st.get("closed") is True, "闸门 __sessionClosed 已落下 (closed=%s)" % st.get("closed"))
        v.check(st.get("natActive") in (0, False), "原生通道已释放 (natActive=%s)" % st.get("natActive"))
        v.check(st.get("player") == "null", "网页侧播放器已销毁 (player=%s)" % st.get("player"))
        v.check(st.get("vsrc") == "empty", "video 源已清空 (vsrc=%s)" % st.get("vsrc"))

    banner("音频轨探针（若上面全绿而这里仍有 active，问题在 Java 侧）")
    for i in range(3):
        n, a, rows = tracks()
        print("  probe %d: started=%d flinger_active=%d" % (i, n, a), flush=True)
        for rr in rows:
            print("     ", rr, flush=True)
        time.sleep(3)

    rc = v.done()
    sh(["shell", "am", "force-stop", PKG])
    print("=== DONE ===", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())

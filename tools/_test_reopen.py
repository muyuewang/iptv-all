# -*- coding: utf-8 -*-
"""回归：BACK 退出后再开另一个频道，画面层能重新挂载。

为什么需要：v2.4.8 给 release() 加了 detachSurface()（把 TextureView 从宿主里
摘掉，修「返回后还看到视频残帧」）。摘了必须能重新挂上 —— 本脚本就是验这个：
两次开台都要有画面，否则等于把「残留帧」换成了「第二次黑屏」。

依赖：tools/tvlab.py
"""
import sys
import time

from tvlab import (arm_native_probe, banner, boot_app, cap, key, native_probe,
                   open_local, page_state, pick_channel, reset_native_probe, sh,
                   tracks, Verdict, wait_ready, PKG)


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


def open_and_wait(cdp, kw, tag, shot, v):
    # 先清上一台的 ready/size，否则本台会"继承"上一台的通过状态（实测踩过）。
    reset_native_probe(cdp)
    print("open(%s): %s" % (kw, pick_channel(cdp, kw)), flush=True)
    # 判据用原生探针的 ready/size（解码器已出帧），不是 NATIVE.active ——
    # 后者在点击瞬间就是 true，等于什么都没验。
    ok = wait_ready(cdp)
    pr = native_probe(cdp)
    n, a, _ = tracks()
    st = page_state(cdp)
    print("%s state: %s  audio=%d/%d  probe=%s" % (tag, st, n, a, pr), flush=True)
    cap(shot)
    v.check(ok, "%s（%s）解码器已出帧 (vsize=%s)" % (tag, kw, pr.get("vsize")))
    v.check("show" in (st.get("pcls") or ""), "%s 播放层仍在显示 (pcls=%s)" % (tag, st.get("pcls")))
    return ok


def main():
    banner("换台 / 二次开频道回归（画面层可重挂载）")
    cdp = boot_app()
    if not cdp:
        print("!! 连不上 devtools", flush=True)
        return 2
    v = Verdict()

    open_local(cdp)
    n = wait_list(cdp)
    print("list:", n, flush=True)
    if n == 0:
        print("!! 列表为空（接口偶发）", flush=True)
        return 3

    # 探针必须在点频道之前布防 —— 它靠包裹 window.__onNativeState 记 ready/size。
    # 漏了这步 wait_ready() 会一路等满超时，把「没布防」误报成「没起播」。
    print("arm:", arm_native_probe(cdp), flush=True)

    banner("A) 开第一个频道")
    open_and_wait(cdp, "广东", "A", "r1_first.png", v)

    banner("BACK 退出播放层")
    key("BACK")
    time.sleep(3)
    st = cdp.json("(function(){return JSON.stringify({"
                  "pcls:document.getElementById('player').className,"
                  "listN:document.querySelectorAll('#list .ch').length});})()")
    print("after BACK:", st, flush=True)
    v.check(bool(st) and "show" not in (st.get("pcls") or ""), "播放层已关闭")
    v.check(bool(st) and (st.get("listN") or 0) > 0, "列表已恢复 (listN=%s)" % (st or {}).get("listN"))

    banner("B) 再开第二个频道")
    open_and_wait(cdp, "湖南", "B", "r2_second.png", v)

    rc = v.done()
    sh(["shell", "am", "force-stop", PKG])
    print("=== DONE ===", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())

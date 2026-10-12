# -*- coding: utf-8 -*-
"""诊断：NativeTV.nativeStats() 在播放中到底返回什么。

怀疑：media3 的 ExoPlayer 有线程校验（必须在创建它的线程调用），而
@JavascriptInterface 的调用跑在 WebView 的 JavaBridge 线程上 → 抛
IllegalStateException → stats() 的 catch 吞掉 → 恒返回 "{}"。
若成立，player.html 里那个 8s「零推进」卡顿看护（bindNativeWatch）
就是**彻底不生效的死代码**。
"""
import time

from tvlab import (arm_native_probe, banner, boot_app, open_local, pick_channel, sh,
                   wait_ready, PKG)


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
    banner("诊断：nativeStats() 返回什么")
    sh(["start-server"])
    sh(["wait-for-device"])
    cdp = boot_app()
    if not cdp:
        print("!! 无 devtools", flush=True)
        return 2
    try:
        open_local(cdp)
        if wait_list(cdp) == 0:
            print("!! 列表空", flush=True)
            return 3
        arm_native_probe(cdp)        # ★ 不布防的话 window.__lab 不存在，
                                     #   wait_ready 必然假阴性（第一版就踩了这个）
        print("open:", pick_channel(cdp, "广东"), flush=True)
        print("ready:", wait_ready(cdp, tries=10, gap=2.0), flush=True)
        for i in range(4):
            time.sleep(1.5)
            r = cdp.eval_js(
                "(function(){try{return String(NativeTV.nativeStats());}"
                "catch(e){return 'EXC:'+String(e);}})()", 10)
            print("nativeStats #%d -> %s" % (i, r), flush=True)
        print("NATIVE.active =", cdp.eval_js("NATIVE.active?1:0", 8),
              "| pos 走 video:", cdp.eval_js(
                  "(function(){var v=document.getElementById('v');return String(v&&v.currentTime);})()", 8),
              flush=True)
    finally:
        sh(["shell", "am", "force-stop", PKG])
    print("=== DONE ===", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""v2.8 底栏「播放信息角标」端到端探针。

验四件事：
  1. 角标拿到真实数据（分辨率 / 帧率 / 带宽估计 bw / 缓冲 bufferedMs）；
  2. 三态循环：点击 → 关(ⓘ) → 简(1080P · 25fps) → 详(1920×1080 · 25fps · ↓x.xM · 缓冲x.xs) → 关；
  3. localStorage 的 tv_stats 跟着写（重启仍生效）；
  4. 退出播放层后刷新定时器停掉（__statT === null）。

用法：python _probe_stats_badge.py
"""
import json
import time

from tvlab import (PKG, arm_native_probe, banner, boot_app, open_local,
                   pick_channel, sh, wait_ready)

BADGE_JS = ("(function(){var b=document.getElementById('statBadge');"
            "if(!b)return 'NO-EL';"
            "return JSON.stringify({txt:b.textContent,cls:b.className});})()")
STATS_JS = ("(function(){try{return String(NativeTV.nativeStats());}"
            "catch(e){return 'EXC:'+String(e);}})()")


def badge(cdp):
    raw = cdp.eval_js(BADGE_JS, 8)
    try:
        return json.loads(raw)
    except Exception:
        return {"txt": "<%s>" % raw, "cls": "?"}


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


def tap(cdp):
    """真的走一次 onclick（模拟用户点角标）。"""
    return cdp.eval_js(
        "(function(){var b=document.getElementById('statBadge');"
        "b.click();return b.textContent;})()", 8)


def main():
    banner("v2.8 播放信息角标")
    sh(["start-server"])
    sh(["wait-for-device"])
    cdp = boot_app()
    if not cdp:
        print("!! 无 devtools", flush=True)
        return 2
    ok = True
    try:
        open_local(cdp)
        if wait_list(cdp) == 0:
            print("!! 列表空", flush=True)
            return 3
        arm_native_probe(cdp)
        print("open:", pick_channel(cdp, "广东"), flush=True)
        print("ready:", wait_ready(cdp, tries=12, gap=2.0), flush=True)

        # ---- 1. 数据是否真的进来了 ----
        print("\n--- 1) 角标 + nativeStats 采样 ---", flush=True)
        for i in range(4):
            time.sleep(1.5)
            b = badge(cdp)
            print("[%d] badge = %-46s cls=%s" % (i, b["txt"], b["cls"]), flush=True)
            print("    stats = %s" % (cdp.eval_js(STATS_JS, 10) or "")[:200], flush=True)
        b = badge(cdp)
        if not b["txt"] or b["txt"] in ("…", "NO-EL"):
            print("!! 角标没有拿到数据", flush=True)
            ok = False
        if "-1fps" in b["txt"]:
            print("!! 显示了无效帧率 -1fps（Format.NO_VALUE 没被过滤）", flush=True)
            ok = False

        # ---- 2. 三态循环 ----
        print("\n--- 2) 三态循环（点击） ---", flush=True)
        print("mode0 =", cdp.eval_js("String(STATS_MODE)", 8), flush=True)
        for k in range(3):
            tap(cdp)
            time.sleep(0.6)
            b = badge(cdp)
            mode = cdp.eval_js("String(STATS_MODE)", 8)
            ls = cdp.eval_js("String(localStorage.getItem('tv_stats'))", 8)
            print("tap%d -> mode=%s ls=%s txt=%-46s cls=%s"
                  % (k + 1, mode, ls, b["txt"], b["cls"]), flush=True)
        # 回到「详」，看带宽/缓冲段在不在
        cdp.eval_js("STATS_MODE=2;updateStatBadge();", 8)
        time.sleep(0.6)
        d = badge(cdp)["txt"]
        print("详模式文本:", d, flush=True)
        if "×" not in d:
            print("!! 详模式没显示 WxH", flush=True)
            ok = False
        if "缓冲" not in d:
            print("!! 详模式没显示缓冲", flush=True)
            ok = False
        if "↓" not in d:
            print("!! 详模式没显示带宽估计（↓）", flush=True)
            ok = False

        # ---- 3. 退出播放层后定时器要停 ----
        print("\n--- 3) 退出播放层 ---", flush=True)
        print("before close: __statT=", cdp.eval_js("String(__statT)", 8), flush=True)
        cdp.eval_js("closePlayer()", 8)
        time.sleep(1.2)
        after = cdp.eval_js("String(__statT)", 8)
        print("after  close: __statT=", after, flush=True)
        if after != "null":
            print("!! 退出播放层后角标定时器没停", flush=True)
            ok = False

        print("\n=== %s ===" % ("PASS" if ok else "FAIL"), flush=True)
        return 0 if ok else 2
    finally:
        sh(["shell", "am", "force-stop", PKG])


if __name__ == "__main__":
    raise SystemExit(main())

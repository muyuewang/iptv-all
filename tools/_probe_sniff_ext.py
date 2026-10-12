"""官网源「网页线路 → 提取直链 → 原生硬解」端到端探针（v2.9）。

验证链路（全链在真机跑）：
  官网页 WebView 自己发 *.m3u8 请求
    → MainActivity.WebViewClient.shouldInterceptRequest 截获（零开销旁听）
    → SniffProbe 后台探测「是否明文可播」（下钻 playlist + 分片首字节）
    → pushSniffResult → 页面 window.__onSniffResult
    → 白名单域名 && 网页稳定起播 3s → playSniffed() 切原生（带 Referer/Cookie）

判定（三条都要过）：
  1) 原生日志出现 CleanTV/SNIFF "hit ..." → 拦截真的生效
  2) 页面 __sniffState().last.ok === true，url 是 .m3u8 → 探测判据正确
  3) 最终 NATIVE.active === true 且 nativeStats 报出真实宽高 → 原生硬解真出画

用法： python tools/_probe_sniff_ext.py [频道关键字]
退出码：0 全过 / 1 有失败
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tvlab import (PKG, arm_native_probe, boot_app, cap, find_ws_url,  # noqa: E402
                   open_wvt, pick_channel, sh)

KW = sys.argv[1] if len(sys.argv) > 1 else "广东"


def sniff_state(cdp):
    r = cdp.eval_js("window.__sniffState?window.__sniffState():'{}'", 10)
    try:
        return json.loads(r) if isinstance(r, str) else (r or {})
    except Exception:
        return {}


def native_stats(cdp):
    r = cdp.eval_js("(window.NativeTV&&NativeTV.nativeStats)?NativeTV.nativeStats():'{}'", 12)
    try:
        return json.loads(r) if isinstance(r, str) else (r or {})
    except Exception:
        return {}


def main():
    sh(["start-server"])
    sh(["wait-for-device"])
    # 清日志，等下按 CleanTV/SNIFF 过滤
    sh(["logcat", "-c"], timeout=20)

    print("=" * 76)
    print("官网源提取探针 · 频道关键字 =", KW)
    cdp = boot_app()
    open_wvt(cdp, delay=5.0)

    # 先看看这个频道有哪些线路（是否含白名单域名）。★ urls 挂在 WVT.byId 上，
    #   WVT.list 的元素只有 id/name/group（实测）。
    lst = cdp.eval_js(
        "(function(){var L=(window.WVT&&WVT.list)||[];var B=(window.WVT&&WVT.byId)||{};var o=[];"
        "for(var i=0;i<L.length;i++){if(L[i].name.indexOf(%s)>=0){var c=B[L[i].id]||{};"
        "var u=((c.urls)||[]).map(function(x){return x.url||x;});"
        "o.push({id:L[i].id,group:L[i].group,urls:u.slice(0,4)});"
        "if(o.length>=4)break;}}return JSON.stringify(o);})()" % json.dumps(KW), 12)
    print("候选频道:", lst[:700] if isinstance(lst, str) else lst, flush=True)

    arm_native_probe(cdp)
    r = pick_channel(cdp, KW)
    print("pick_channel ->", r, flush=True)
    if r != "open":
        # 官网源要先进分组：默认分组可能不含该台，直接按 id 打开
        found = cdp.eval_js(
            "(function(){var L=(window.WVT&&WVT.list)||[];"
            "for(var i=0;i<L.length;i++){if(L[i].name.indexOf(%s)>=0){"
            "var c=L[i];window.openChannel('wvt',c.id,c.name);return c.name;}}"
            "return 'none';})()" % json.dumps(KW), 12)
        print("直接 openChannel ->", found, flush=True)

    # ★ 官网线路走的是 WebView 里的官网页，不是原生通道 → __lab 永远不会 ready。
    #   所以这里等的是「网页侧起播」（webmask 收起 / webState 稳定在 1）。
    for t in range(16):
        time.sleep(2.0)
        st = sniff_state(cdp)
        mask = cdp.eval_js("(document.getElementById('webmask')||{}).className||''", 8)
        na = st.get("nativeActive")
        print("[%02d] webState=%s mask=%s nativeActive=%s cutFor=%s"
              % (t, st.get("webState"), ("show" in str(mask)) and "show" or "-",
                 na, (st.get("cutFor") or "")[:52]), flush=True)
        if na:
            break

    last = st.get("last") or {}
    ns = native_stats(cdp)
    print("\n--- __sniffState().last ---")
    print(json.dumps(last, ensure_ascii=False)[:400])
    print("--- nativeStats ---")
    print(json.dumps(ns, ensure_ascii=False)[:300])

    # logcat：拦截与探测日志（★ tag 含 "/"，用 -d 全量再在 Python 侧过滤更稳）
    log = sh(["logcat", "-d"], timeout=30) or ""
    hits = [l for l in log.splitlines() if "SNIFF" in l]
    print("\n--- logcat SNIFF（%d 行）---" % len(hits))
    for l in hits[-16:]:
        print("   ", l.strip()[:175])

    cap("sniff_ext.png")
    # ★ 切换后官方页 WebView 应当已被拆掉（否则它的页面残留会从透明主 WebView 下透出来）
    time.sleep(4.0)
    cap("sniff_after.png")
    left = find_ws_url(port=9337, match="gdtv")
    print("\n切换后官方页 target:", left or "（已销毁 ✓）", flush=True)
    sh(["shell", "am", "force-stop", PKG])

    # ---- 判定 ----
    r1 = any("hit#" in l or "hit " in l for l in hits)
    r2 = bool(last.get("ok")) and (".m3u8" in str(last.get("url", "")))
    r3 = bool(st.get("nativeActive")) and bool(ns.get("w")) and bool(ns.get("h"))
    print("\n================ 判定 ================")
    print("① 拦截生效（shouldInterceptRequest 截到 m3u8）:", "PASS" if r1 else "FAIL")
    print("② 探测判定可播（__onSniffResult.ok）      :", "PASS" if r2 else "FAIL",
          "| url=", str(last.get("url", ""))[:80])
    print("③ 切原生并出画（active + 真实宽高）       :", "PASS" if r3 else "FAIL",
          "| %sx%s" % (ns.get("w"), ns.get("h")))
    if not r1:
        print("   提示：若 hit 一行都没有，说明该站点的流不是 WebView 自己请求的"
              "（例如站点用 JS 拼地址后走原生播放器），需换站点再测。")
    return 0 if (r1 and r2 and r3) else 1


if __name__ == "__main__":
    sys.exit(main())

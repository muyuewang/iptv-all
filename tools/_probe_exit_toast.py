# -*- coding: utf-8 -*-
"""诊断：退出播放层时，Toast 有没有残留。

背景：`_smoke_v25.py` 收尾断言「回列表后 toasts==0」失败了（toasts=1）。
`closePlayer()` 里调了 `hideErrCard()` 却没清 Toast —— 需要判明这是
「用假频道造出来的测试假象」还是「真实路径也会留」。

两条路径分开测：
  A) 真实频道：起播 → BACK → 看 toasts
  B) 失败路径：假频道 openChannel（必然全线路失败）→ 等失败提示出现 → BACK → 看 toasts

依赖：tools/tvlab.py
"""
import sys
import time

from tvlab import (arm_native_probe, banner, boot_app, cap, key, open_local,
                   page_state, pick_channel, sh, wait_ready, PKG)

DUMP = ("(function(){var box=document.getElementById('toasts');"
        "var els=box?box.querySelectorAll('.tst'):[];var out=[];"
        "for(var i=0;i<els.length;i++){"
        "  out.push({cls:els[i].className,txt:els[i].textContent,"
        "            btn:(els[i].querySelector('.act')||{}).textContent||''});}"
        "return JSON.stringify({n:els.length,list:out,"
        "card:(document.getElementById('errCard')||{className:''}).className});})()")


def dump(cdp, tag):
    d = cdp.json(DUMP) or {}
    print("[%s] toasts=%s card=%s" % (tag, d.get("n"), d.get("card")), flush=True)
    for t in (d.get("list") or []):
        print("      · %-14s %r btn=%r" % (t.get("cls"), t.get("txt"), t.get("btn")), flush=True)
    return d


def main():
    cdp = boot_app()
    if not cdp:
        print("!! 连不上 devtools", flush=True)
        return 2
    res = {}

    banner("A) 真实频道：起播 → BACK")
    open_local(cdp)
    arm_native_probe(cdp)
    print("open(广东):", pick_channel(cdp, "广东"), flush=True)
    print("起播:", wait_ready(cdp), page_state(cdp), flush=True)
    time.sleep(2.0)
    dump(cdp, "A-播放中")
    key("BACK")
    time.sleep(2.5)
    res["A"] = dump(cdp, "A-BACK 后")
    cap("probe_toast_A.png")

    banner("B) 失败路径：假频道 → 全线路失败 → BACK")
    cdp.eval_js("(function(){openChannel('tv','x','演示频道');return 1;})()", 10)
    time.sleep(9)                      # 等兜底链跑到「全部失败」并弹提示
    dump(cdp, "B-失败后")
    key("BACK")
    time.sleep(2.5)
    res["B"] = dump(cdp, "B-BACK 后")
    cap("probe_toast_B.png")

    banner("结论")
    ra = res["A"].get("n") or 0
    rb = res["B"].get("n") or 0
    print("A) 真实频道退出后残留 Toast 数 = %d  → %s" % (ra, "干净" if ra == 0 else "★ 有残留"),
          flush=True)
    print("B) 失败后退出残留 Toast 数     = %d  → %s" % (rb, "干净" if rb == 0 else "★ 有残留"),
          flush=True)
    sh(["shell", "am", "force-stop", PKG])
    print("=== DONE ===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""缓冲档位隔离基准：同一 URL、同一次会话内交替 fast/stable，只测
   「nativePlay 调用 → ExoPlayer 首次 ready」这一段。

为什么这样测：完整「点开到出画」的耗时里，接口请求 + 并发测速 + 镜像切换
占了绝大多数且方差极大（实测 5.9s ~ 39s），会把 bufferForPlaybackMs 那
几百毫秒的差异完全淹没。把测速与选路排除掉，才看得见档位本身的效果。

用法：python -u tools/_bench_buf.py [每档轮数]
环境变量：TV_BENCH_CH 换频道（默认 广东）

（原来自带一份 CDP 客户端，已改为复用 tools/tvlab.py。）
"""
import json
import os
import sys
import time

from tvlab import (arm_native_probe, arm_start_timer, banner, boot_app,
                   mark_start, open_local, pick_channel, sh, start_ms, PKG)

PORT = 9337
CH = os.environ.get("TV_BENCH_CH", "广东")
ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 5


def main():
    banner("缓冲档位隔离基准：频道=%s 每档 %d 轮" % (CH, ROUNDS))
    cdp = boot_app(port=PORT)
    if not cdp:
        print("!! 连不上 devtools", flush=True)
        return 2
    arm_native_probe(cdp)
    arm_start_timer(cdp)
    open_local(cdp)
    print("open:", pick_channel(cdp, CH), flush=True)
    time.sleep(8)
    url = cdp.eval_js("(function(){return (cur&&cur.urls&&cur.urls[cur.idx])||'';})()", 8)
    if not url or not str(url).startswith("http"):
        print("!! 拿不到线路地址:", url, flush=True)
        return 3
    print("URL(截断):", str(url)[:70], flush=True)

    res = {"fast": [], "stable": []}
    for i in range(ROUNDS):
        for tune in ("fast", "stable"):        # 交替，抵消网络漂移
            cdp.eval_js("(function(){setTune('%s');return 1;})()" % tune)
            time.sleep(0.25)
            mark_start(cdp)
            cdp.eval_js(
                "(function(){NATIVE.active=false; NATIVE.dead=false;"
                "NativeTV.nativePlay(%s);"
                "NATIVE.active=true;return 1;})()" % json.dumps(url), 12)
            ms = start_ms(cdp, timeout=25)
            nb = cdp.eval_js("(window.NativeTV&&NativeTV.getTuneProfile)?NativeTV.getTuneProfile():'n/a'", 8)
            res[tune].append(ms)
            print("  [%s #%d] %dms (native=%s)" % (tune, i + 1, ms, nb), flush=True)
            time.sleep(0.8)

    banner("汇总（nativePlay → 首次 ready）")
    med = {}
    for k in ("fast", "stable"):
        v = sorted(x for x in res[k] if x >= 0)
        med[k] = v[len(v) // 2] if v else -1
        print("  %-6s n=%d 中位数=%dms 样本=%s" % (k, len(v), med[k], v), flush=True)
    if med["fast"] >= 0 and med["stable"] >= 0:
        print("  中位数差(stable-fast) = %+dms" % (med["stable"] - med["fast"]), flush=True)
    cdp.eval_js("(function(){closePlayer();return 1;})()")
    sh(["shell", "am", "force-stop", PKG])
    return 0


if __name__ == "__main__":
    sys.exit(main())

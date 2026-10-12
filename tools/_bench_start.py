# -*- coding: utf-8 -*-
"""起播耗时 A/B 基准：秒开(fast) vs 稳定(stable)

测法：每次冷启动 → 打开同一频道 → 记录「点开到 ExoPlayer 首次 ready」的耗时
（在页面里包一层 __onNativeState 打时间戳，不改业务逻辑）。
两种档位各跑 N 轮，输出中位数与全部样本。

用法：python -u tools/_bench_start.py [轮数]
环境变量：TV_BENCH_CH 换频道（默认 广东）

（原来自带一份 CDP 客户端，已改为复用 tools/tvlab.py。）
"""
import os
import sys
import time

from tvlab import (arm_native_probe, arm_start_timer, banner, boot_app,
                   mark_start, open_local, pick_channel, sh, start_ms, PKG)

PORT = 9336
CH = os.environ.get("TV_BENCH_CH", "广东")
ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 3


def run_once(tune):
    """冷启动一次，跑一轮，返回 {'ms':..,'tune':..,'native':..} 或 None。"""
    cdp = boot_app(port=PORT)
    if not cdp:
        print("!! 连不上 devtools", flush=True)
        return None
    arm_native_probe(cdp)          # 先装状态探针
    arm_start_timer(cdp)           # 再链一层计时（顺序不能反）
    open_local(cdp)
    cdp.eval_js("(function(){setTune('%s');return 1;})()" % tune)
    time.sleep(0.4)
    native = cdp.eval_js("(window.NativeTV&&NativeTV.getTuneProfile)?NativeTV.getTuneProfile():'n/a'")

    mark_start(cdp)                # 打起点，紧接着点台
    r = pick_channel(cdp, CH)
    if r != "open":
        print("  !! 找不到频道:", r, flush=True)
        cdp.eval_js("(function(){closePlayer();return 1;})()")
        return None
    ms = start_ms(cdp, timeout=45)
    panel = cdp.json(
        "(function(){return JSON.stringify({tune:TUNE,"
        "native:(window.NativeTV&&NativeTV.getTuneProfile)?NativeTV.getTuneProfile():'n/a'});})()",
        8) or {}
    cdp.eval_js("(function(){closePlayer();return 1;})()")
    time.sleep(0.6)
    return {"ms": ms, "tune": panel.get("tune"), "native": native}


def main():
    banner("起播基准：频道=%s 每档 %d 轮" % (CH, ROUNDS))
    res = {"fast": [], "stable": []}
    for tune in ("fast", "stable"):
        for i in range(ROUNDS):
            r = run_once(tune)
            if r and r["ms"] >= 0:
                res[tune].append(r["ms"])
                print("  [%s #%d] %dms  (TUNE=%s native=%s)"
                      % (tune, i + 1, r["ms"], r["tune"], r["native"]), flush=True)
            else:
                print("  [%s #%d] 失败" % (tune, i + 1), flush=True)
    banner("汇总（点开 → 首次 ready）")
    med = {}
    for k in ("fast", "stable"):
        v = sorted(res[k])
        med[k] = v[len(v) // 2] if v else -1
        print("  %-6s n=%d 中位数=%dms 样本=%s" % (k, len(v), med[k], v), flush=True)
    if med["fast"] >= 0 and med["stable"] >= 0:
        print("  差值(stable-fast) = %+dms" % (med["stable"] - med["fast"]), flush=True)
    sh(["shell", "am", "force-stop", PKG])
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""
生成 shared/wvt_builtin.json —— 随包发布的「已解密 WebViewTV 频道表快照」。

用法（在 tv_all 目录下执行）：
    python tools/gen_wvt_snapshot.py

什么时候要重新跑：
    · WebViewTV 官方频道表大幅更新（一般每月一两次）
    · 发版前刷新一次，保证离线首启也有较新的频道
"""
import os
import sys
import json
import time
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SHARED = os.path.join(ROOT, "shared")
OUT = os.path.join(SHARED, "wvt_builtin.json")


def load_wvt():
    p = os.path.join(SHARED, "wvt.py")
    spec = importlib.util.spec_from_file_location("wvt", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    W = load_wvt()
    print("下载并解密频道表 ...")
    chans = W.load_channels(force=True)
    print("  频道数:", len(chans))
    print("下载并解密适配规则 ...")
    ads = W.load_adapters(force=True)
    print("  规则数:", len(ads))
    if not chans:
        print("!! 频道表为空，放弃写入")
        return 1
    snap = {
        "generated": int(time.time()),
        "generated_text": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "https://github.com/hxh19950701/WebViewTvLive (data/4)",
        "channels": chans,
        "adapters": ads,
    }
    # 央视频付费频道标记（限免/VIP，pid -> tag）：重新生成时保留旧值。
    # 刷新方式见 tools/_scrape_ysp_tags.py（真浏览器抓央视频 TV 页侧栏标签）。
    try:
        with open(OUT, "r", encoding="utf-8") as f:
            _old_tags = json.load(f).get("tags")
        if _old_tags:
            snap["tags"] = _old_tags
    except Exception:
        pass
    # 压缩存储：去掉 channels 里空的 urls / 空 extra，减小体积
    for c in snap["channels"]:
        for u in c.get("urls", []):
            if not u.get("extra"):
                u.pop("extra", None)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, separators=(",", ":"))
    size = os.path.getsize(OUT)
    print("已写入 %s  (%.1f KB)" % (OUT, size / 1024.0))
    d, w = W.split_sources(chans)
    print("直连线路 %d / 网页线路 %d" % (len(d), len(w)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

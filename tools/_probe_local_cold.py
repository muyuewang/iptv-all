# -*- coding: utf-8 -*-
"""
探针：本机源冷启动耗时（fetch_channels 各分类）。

背景：用户反馈 PC 版首次双击后「只有本机源，等了一会后才有」。
本探针量化「首次（无内存缓存）拉一个分类要多久」「第二次（命中内存缓存）」。
"""
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "shared"))

import importlib.util as ilu

spec = ilu.spec_from_file_location("cleantv_core", os.path.join(_ROOT, "shared", "core.py"))
core = ilu.module_from_spec(spec)
spec.loader.exec_module(core)

print("=" * 60)
print("本机源冷启动探针")
print("=" * 60)
print("API:", core.API)
print("CATS:", core.CATS)
print()

mem = {}
total0 = time.time()
for tid, name in list(core.CATS):
    t0 = time.time()
    try:
        chans = core.fetch_channels(tid, cache=mem)
        dt = time.time() - t0
        print("  %-6s %-10s %6.2fs  %3d 个频道" % (tid, name, dt, len(chans)))
    except Exception as e:
        print("  %-6s %-10s %6.2fs  ✗ %r" % (tid, name, time.time() - t0, e))
print("合计（冷）: %.2fs" % (time.time() - total0))

print()
t0 = time.time()
core.fetch_channels("tv", cache=mem)
print("第二次 fetch_channels('tv')（内存缓存命中）: %6.3fs" % (time.time() - t0))

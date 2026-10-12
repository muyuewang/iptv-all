# -*- coding: utf-8 -*-
"""
探针：模拟「别人电脑首次双击 exe」的官网源冷启动 —— 量化各阶段耗时。

用途：定位「官网源打开后长时间没有节目列表」到底卡在哪一步。
跑法：python tools/_probe_wvt_cold.py [--keep]
"""
import os
import sys
import time
import json
import tempfile
import shutil

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "shared"))

import importlib.util as ilu

spec = ilu.spec_from_file_location("cleantv_wvt", os.path.join(_ROOT, "shared", "wvt.py"))
wvt = ilu.module_from_spec(spec)
spec.loader.exec_module(wvt)


def t(label, fn):
    t0 = time.time()
    try:
        r = fn()
        dt = time.time() - t0
        return r, dt, None
    except Exception as e:
        return None, time.time() - t0, e


def main():
    keep = "--keep" in sys.argv
    cold = tempfile.mkdtemp(prefix="wvtcold_")
    print("=" * 64)
    print("官网源冷启动探针（cache_dir = 空目录，模拟首次运行）")
    print("=" * 64)
    print("cache_dir:", cold)
    print()

    # ---- 0. 内置快照 ----
    r, dt, e = t("builtin", wvt.builtin_snapshot)
    snap = r or {}
    print("[0] builtin_snapshot()          %6.3fs   channels=%s  adapters=%s  tags=%s"
          % (dt, len(snap.get("channels") or []), len(snap.get("adapters") or []),
             len(snap.get("tags") or {})))
    if e:
        print("     ✗ %r" % (e,))

    # ---- 1. config ----
    r, dt, e = t("config", lambda: wvt.load_config(cache_dir=cold))
    print("[1] load_config()               %6.3fs   %s"
          % (dt, ("ok, proxies=%d" % len(r.get("githubProxies") or [])) if r else "空"))
    if e:
        print("     ✗ %r" % (e,))

    # ---- 2. channels ----
    r, dt, e = t("channels", lambda: wvt.load_channels(cache_dir=cold))
    n = len(r or [])
    print("[2] load_channels()             %6.3fs   %d 个频道" % (dt, n))
    if e:
        print("     ✗ %r" % (e,))

    # ---- 3. adapters ----
    r, dt, e = t("adapters", lambda: wvt.load_adapters(cache_dir=cold))
    print("[3] load_adapters()             %6.3fs   %d 条规则" % (dt, len(r or [])))
    if e:
        print("     ✗ %r" % (e,))

    # ---- 4. 写入的文件 ----
    print()
    print("冷启动在 cache_dir 里写了：")
    for f in sorted(os.listdir(cold)):
        p = os.path.join(cold, f)
        print("   %-28s %8d bytes" % (f, os.path.getsize(p)))

    # ---- 5. 热启动（第二次，有缓存） ----
    print()
    r, dt, e = t("warm", lambda: wvt.load_channels(cache_dir=cold))
    print("[4] 第二次 load_channels()      %6.3fs   %d 个频道（热启动）" % (dt, len(r or [])))

    if not keep:
        shutil.rmtree(cold, ignore_errors=True)
    else:
        print("\n（--keep：目录保留在 %s）" % cold)


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""
探针：PC 版「别人电脑首次双击」冷启动时序。

背景：用户反馈「pc 版直接发给别人、双击打开后官网源没节目列表、只有本机源
等了一会后才有」。本探针跑**真实 GUI**（系统 Python 才有 tkinter），
把 DATA_DIR 指到一个空临时目录（= 全新电脑，无任何缓存），记录：

  t0            导入结束
  t1            窗口构造完成（此时官网源列表应当已经上屏）
  t2..tn        每次 _after_wvt_load 回调（谁先谁后、多少频道、静默否）
  列表上屏时刻    _render_channels 首次收到非空列表

跑法：
  D:/devtool/python-3.13.0/python.exe tools/_probe_pc_cold_start.py
环境变量：
  PROBE_DUR=35     跑多少秒后自动关窗
  PROBE_NET=off    模拟「完全连不上网」（把下载函数打成必抛异常）
"""
import os
import sys
import time
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "desktop"))

DUR = float(os.environ.get("PROBE_DUR", "20"))
NET = (os.environ.get("PROBE_NET", "on") or "on").lower()

T_IMPORT0 = time.time()
import native_app                                  # noqa: E402
T_IMPORT = time.time() - T_IMPORT0

# ---- 模拟新电脑：空的运行时目录（settings/fav/logocache/wvt 缓存全从零）----
COLD = tempfile.mkdtemp(prefix="cleantv_cold_")
native_app.DATA_DIR = COLD

if NET == "off":
    def _dead_download(rel, proxies=None, timeout=None, budget=None):
        raise OSError("PROBE_NET=off：模拟网络不可达")
    native_app._wvt.download_dat = _dead_download

# PROBE_MUTATE=drop5：让「联网刷新」返回一份**少了 5 个央视频道**的表，
# 用来验证「静默刷新遇到数据变化时必须重建列表」（只验「不变就不动」不够）。
if (os.environ.get("PROBE_MUTATE") or "") == "drop5":
    _orig_load_channels = native_app._wvt.load_channels

    def _mutated(cache_dir=None, ttl=None, force=False, proxies=None):
        out = []
        dropped = 0
        for c in _orig_load_channels(cache_dir=cache_dir, force=force, proxies=proxies):
            if (c.get("group") == "央视") and dropped < 5:
                dropped += 1
                continue
            out.append(c)
        print("   [mutate] 联网返回的央视频道少了 %d 个（共 %d）" % (dropped, len(out)))
        return out

    native_app._wvt.load_channels = _mutated

calls = []
renders = []
T0 = time.time()


def _el():
    return round(time.time() - T0, 3)


_orig_after = native_app.NativeApp._after_wvt_load


def spy_after(self, chans, ads=None, silent=False, pending=False):
    calls.append({"t": _el(), "n": len(chans or []), "silent": silent,
                  "pending": pending, "grp": self.cur_group,
                  "rows": len(getattr(self, "filtered", []) or [])})
    return _orig_after(self, chans, ads, silent, pending)


_orig_render = native_app.NativeApp._render_channels


def spy_render(self, chans):
    n = len(chans or [])
    if n:
        renders.append((_el(), n))          # 每次「画出非空列表」都记一笔
    return _orig_render(self, chans)


native_app.NativeApp._after_wvt_load = spy_after
native_app.NativeApp._render_channels = spy_render

app = native_app.NativeApp()
T_CTOR = time.time() - T0

state_at_ctor = {
    "wvt_channels": len(getattr(app, "wvt_channels", []) or []),
    "filtered": len(getattr(app, "filtered", []) or []),
    "groups": len(getattr(app, "wvt_groups", []) or []),
    "status": "",
}
try:
    state_at_ctor["status"] = app.status_lbl.cget("text")
except Exception:
    pass


def finish():
    try:
        app.root.quit()
    except Exception:
        pass


app.root.after(int(DUR * 1000), finish)
try:
    app.run()
except Exception as e:                              # noqa: BLE001
    print("run() 异常：%r" % (e,))

final = {
    "wvt_channels": len(getattr(app, "wvt_channels", []) or []),
    "filtered": len(getattr(app, "filtered", []) or []),
    "groups": len(getattr(app, "wvt_groups", []) or []),
    "status": "",
}
try:
    final["status"] = app.status_lbl.cget("text")
except Exception:
    pass

# ------------------------------------------------------------------ 报告
print("=" * 70)
print("PC 冷启动时序探针   DATA_DIR=%s" % COLD)
print("网络模式：%s（off = 模拟完全连不上）  观察时长 %.0fs" % (NET, DUR))
print("=" * 70)
print("导入耗时            %6.2fs" % T_IMPORT)
print("窗口构造完成        %6.2fs" % T_CTOR)
print("构造完成时          wvt_channels=%d  filtered=%d  groups=%d  status=%r"
      % (state_at_ctor["wvt_channels"], state_at_ctor["filtered"],
         state_at_ctor["groups"], state_at_ctor["status"]))
print()
print("_after_wvt_load 回调序列：")
for c in calls:
    print("   t=%7.3fs  频道=%-4d silent=%-5s pending=%-5s 分组=%-6s 已渲染行=%d"
          % (c["t"], c["n"], c["silent"], c["pending"], c["grp"], c["rows"]))
print()
print("_render_channels 非空列表时刻：")
for t, n in renders[:6]:
    print("   t=%7.3fs  %d 行" % (t, n))
print()
print("-" * 70)
if renders:
    print("★ 列表首次上屏：%.2f 秒" % renders[0][0])
else:
    print("★ 列表始终没有上屏（空）")
print("★ %.0fs 后：官网源 %d 频道 / %d 分组，当前显示 %d 行"
      % (DUR, final["wvt_channels"], final["groups"], final["filtered"]))
print("★ 最终状态栏：%r" % final["status"])
print("-" * 70)
print()
print("（运行时目录 %s 保留，便于检查缓存写入）" % COLD)

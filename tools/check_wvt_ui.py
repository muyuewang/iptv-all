# -*- coding: utf-8 -*-
"""官网源 UI 接入冒烟测试（不拉起 mpv / 不真嗅探 / 不真开浏览器）。

覆盖：
  * 源切换（本机源 <-> 官网源）与胶囊标签条状态同步
  * 官网源分组下拉 / 频道表归一化
  * 直连线路 -> mpv
  * 网页线路 -> 内嵌浏览器（_show_web，已打桩）
  * 无浏览器时的退路：嗅探 -> mpv（含请求头透传）
  * 过期嗅探结果丢弃
  * 全屏进出（布局 pack / 恢复）

用法：python tools/check_wvt_ui.py
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "desktop"))

import native_app as N  # noqa: E402

FAIL = []
T0 = time.time()


def check(cond, msg):
    print(("  [OK] " if cond else "  [!!] ") + msg)
    sys.stdout.flush()
    if not cond:
        FAIL.append(msg)


def info(msg):
    print("  " + msg)
    sys.stdout.flush()


print("== 1) 初始化 NativeApp ==")
app = N.NativeApp()

# 收集 tkinter 回调异常（不能刷屏、不能静默吞掉）
TKE = []


def _report(exc, val, tb):
    import traceback as _tb
    TKE.append("%s: %s" % (exc.__name__, val))
    print("  [TK-ERR] %s: %s" % (exc.__name__, val))
    _tb.print_exception(exc, val, tb)


app.root.report_callback_exception = _report

played = []
app._start_mpv = lambda url, headers=None: played.append((url, headers))
opened = []                       # 网页线路 -> 内嵌浏览器（打桩）
app._show_web = lambda url, extra="": opened.append((url, extra))

sniffed = []
if N._cdp is not None:
    def fake_sniff(page_url, **kw):
        sniffed.append(page_url)
        return {"url": "https://cdn.example.com/live/index.m3u8?t=1",
                "headers": {"referer": page_url, "user-agent": "UA/1.0",
                            "cookie": "a=b", "x-ignored": "z"}}
    N._cdp.sniff_stream = fake_sniff
    info("cdp 已载入，sniff_stream 已打桩")
else:
    info("[!!] cdp 不可用，退路嗅探无法测试")

info("内嵌浏览器模块：%s" % ("可用" if N._embed_mod is not None else "不可用"))
info("PillBar 胶囊条：src_bar / cat_bar 已建立")

STATE = {"direct": [], "web": [], "g": None, "c2": None}


def finish():
    print()
    check(not TKE, "tkinter 回调无异常（实际 %d 个）" % len(TKE))
    print("总用时 %.1fs" % (time.time() - T0))
    if FAIL:
        print("失败 %d 项：" % len(FAIL))
        for f in FAIL:
            print("  - " + f)
        code = 2
    else:
        print("全部通过 ✅")
        code = 0
    try:
        app.root.destroy()
    except Exception:
        pass
    sys.exit(code)


# ---------------------------------------------------------------- 2) 切源
def step2():
    print("== 2) 切到官网源，等待频道表 ==")
    STATE["t"] = time.time()
    check(app.src_bar.active == "wvt", "初始高亮在官网源（v2.4.2 官网源默认+第一位）")
    check(app.src_bar.items[0][0] == "wvt", "官网源胶囊在第一位")
    app._set_src("wvt")               # 已在官网源：应直接返回，不影响后续
    check(app.src_mode == "wvt", "src_mode == wvt")
    check(app.src_bar.active == "wvt", "官网源胶囊已高亮（PillBar.active）")
    check(app.cat_bar.winfo_ismapped() == 0, "分类胶囊已收起")
    check(bool(app.group_frame.winfo_manager()), "分组下拉已显示")
    poll_wvt(0)


def poll_wvt(n):
    if app.wvt_loaded:
        info("频道表加载用时 %.1fs" % (time.time() - STATE["t"]))
        step2b()
    elif n > 1800:                       # 180s
        check(False, "频道表加载超时")
        finish()
    else:
        app.root.after(100, lambda: poll_wvt(n + 1))


def step2b():
    n_ch = len(app.wvt_channels)
    n_g = len(app.wvt_groups)
    info("频道 %d 个 / 分组 %d 个" % (n_ch, n_g))
    check(n_ch > 800, "频道数 > 800（实际 %d）" % n_ch)
    check(n_g > 30, "分组数 > 30（实际 %d）" % n_g)
    check(len(app.group_cbo.cget("values")) == n_g, "下拉框分组数与分组列表一致")
    check(len(app.wvt_adapters) > 0, "网页适配规则已载入（%d 条）" % len(app.wvt_adapters))
    STATE["direct"] = [c for c in app.wvt_channels
                       if N._wvt.is_direct_stream(c["urls"][0])]
    STATE["web"] = [c for c in app.wvt_channels
                    if not N._wvt.is_direct_stream(c["urls"][0])]
    info("直连线路 %d 条 / 网页线路 %d 条" % (len(STATE["direct"]), len(STATE["web"])))
    check(bool(STATE["direct"]), "存在直连频道")
    check(bool(STATE["web"]), "存在网页频道")
    app.root.after(100, step3)


# ---------------------------------------------------------------- 3) 分组
def step3():
    print("== 3) 分组切换 ==")
    g = "央视" if "央视" in app.wvt_groups else app.wvt_groups[0]
    STATE["g"] = g
    app._load_group(g)
    app.root.after(300, step3b)


def step3b():
    g = STATE["g"]
    check(app.cur_group == g, "cur_group == %s" % g)
    check(len(app.channels) > 0, "分组内频道数 > 0（%d）" % len(app.channels))
    check(g in app.status_lbl.cget("text"), "状态栏：%s" % app.status_lbl.cget("text"))
    info("前 6 个分组：%s" % "、".join(app.wvt_groups[:6]))
    app.root.after(100, step4)


# ---------------------------------------------------------------- 4) 直连
def step4():
    print("== 4) 直连频道应直接交给 mpv ==")
    c = STATE["direct"][0]
    check(N._wvt.is_direct_stream(c["urls"][0]),
          "样本 %s -> %s" % (c["name"], c["urls"][0][:70]))
    app._open_channel(c["tid"], c["id"], c["name"])
    app.root.after(500, step4b)


def step4b():
    check(bool(played), "直连频道已交给 mpv")
    if played:
        info("mpv <- %s" % played[-1][0][:80])
    check(app.cur["src"] == "wvt", "cur.src == wvt")
    check(not opened, "直连线路未打开内嵌浏览器")
    check(not sniffed, "直连线路未触发嗅探")
    app.root.after(100, step5)


# ---------------------------------------------------------------- 5) 网页 -> 内嵌浏览器
def step5():
    print("== 5) 网页线路 -> 内嵌浏览器（_show_web）==")
    played.clear()
    opened.clear()
    c2 = None
    for cc in STATE["web"]:
        if cc["group"] == app.cur_group:
            c2 = cc
            break
    c2 = c2 or STATE["web"][0]
    STATE["c2"] = c2
    info("样本 %s（%s）" % (c2["name"], c2["urls"][0][:70]))
    app._open_channel(c2["tid"], c2["id"], c2["name"])
    app.root.after(600, step5b)


def step5b():
    check(len(opened) == 1, "内嵌浏览器被调用 1 次（实际 %d）" % len(opened))
    if opened:
        check(opened[0][0] == STATE["c2"]["urls"][0], "打开的是该线路地址")
        info("inject: %s" % (opened[0][1] or "(空)"))
    check(not sniffed, "有内嵌浏览器时不走嗅探退路")
    check(not played, "网页线路不进 mpv")
    app.root.after(100, step5c)


# ---------------------------------------------------------------- 5c) 退路嗅探
def step5c():
    print("== 5c) 无内嵌浏览器时的退路：嗅探 -> mpv ==")
    if N._cdp is None:
        info("跳过（cdp 不可用）")
        app.root.after(100, step6)
        return
    played.clear()
    sniffed.clear()
    app._show_web = lambda url, extra="": app._sniff_fallback(url, extra)
    app._open_channel(STATE["c2"]["tid"], STATE["c2"]["id"], STATE["c2"]["name"])
    app.root.after(1500, step5d)


def step5d():
    check(bool(played), "嗅探结果已交给 mpv")
    check(len(sniffed) == 1, "sniff_stream 调用 1 次（实际 %d）" % len(sniffed))
    if played:
        url, hdrs = played[-1]
        check(url.startswith("https://cdn.example.com/"), "播放的是嗅探地址")
        hf = app._header_fields(hdrs)
        check("referer:" in hf and "cookie:" in hf, "请求头带上了 referer+cookie")
        check("x-ignored" not in hf, "无关请求头被过滤")
        info("header-fields = %s" % hf)
    app._show_web = lambda url, extra="": opened.append((url, extra))
    app.root.after(100, step6)


# ---------------------------------------------------------------- 6) 过期
def step6():
    print("== 6) 过期嗅探结果应被丢弃 ==")
    if N._cdp is None:
        info("跳过（cdp 不可用）")
        app.root.after(100, step7)
        return
    played.clear()
    N._cdp.sniff_stream = lambda page_url, **kw: None
    c2 = STATE["c2"]
    app._open_channel(c2["tid"], c2["id"], c2["name"])
    app.root.after(700, step6b)


def step6b():
    check(not played, "解析中：暂无播放")
    stale = app._req - 1
    c2 = STATE["c2"]
    app._after_sniff({"url": "https://old.example.com/x.m3u8"}, c2["urls"][0], stale)
    app.root.after(300, step6c)


def step6c():
    check(not played, "过期结果未触发播放")
    c2 = STATE["c2"]
    app._after_sniff({"url": "https://new.example.com/y.m3u8"}, c2["urls"][0], app._req)
    app.root.after(300, step6d)


def step6d():
    check(len(played) == 1, "当前请求号的结果正常播放（%d）" % len(played))
    app.root.after(100, step7)


# ---------------------------------------------------------------- 7) 全屏
def step7():
    print("== 7) 全屏进出（布局应正确收起 / 恢复）==")
    check(not app._fullscreen, "初始非全屏")
    check(app.top_bar.winfo_ismapped() == 1, "顶栏可见")
    app._toggle_fullscreen()
    app.root.after(500, step7b)


def step7b():
    check(app._fullscreen, "已进入全屏")
    check(app.top_bar.winfo_ismapped() == 0, "全屏后顶栏收起")
    check(app.ctrl_bar.winfo_ismapped() == 0, "全屏后控制条收起")
    check(app.left_panel.winfo_ismapped() == 0, "全屏后左侧列表收起")
    check(app.video_host.winfo_ismapped() == 1, "播放区仍在")
    app._exit_fullscreen()
    app.root.after(700, step7c)


def step7c():
    check(not app._fullscreen, "已退出全屏")
    check(app.top_bar.winfo_ismapped() == 1, "顶栏恢复")
    check(app.ctrl_bar.winfo_ismapped() == 1, "控制条恢复")
    check(app.left_panel.winfo_ismapped() == 1, "左侧列表恢复")
    app.root.after(100, step8)


# ---------------------------------------------------------------- 8) 回本机源 + 搜索
def step8():
    print("== 8) 切回本机源 ==")
    app._set_src("local")
    app.root.after(600, step8b)


def step8b():
    check(app.src_mode == "local", "src_mode == local")
    check(app.src_bar.active == "local", "本机源胶囊已高亮")
    check(app.cat_bar.winfo_ismapped() == 1, "分类胶囊恢复")
    check(not app.group_frame.winfo_manager(), "分组下拉已隐藏")
    check(app.cat_bar.active == "tv", "分类回到了「综合」")
    app.root.after(100, step9)


def step9():
    print("== 9) 官网源搜索过滤 ==")
    app._set_src("wvt")
    app.root.after(400, step9b)


def step9b():
    app._load_group(STATE["g"])
    app.search_var.set("CCTV")
    app._on_search()
    app.root.after(400, step9c)


def step9c():
    info("搜索 'CCTV' 在分组「%s」命中 %d 个" % (STATE["g"], len(app.filtered)))
    check(len(app.filtered) > 0, "搜索有命中（%d）" % len(app.filtered))
    finish()


app.root.after(400, step2)
app.root.mainloop()

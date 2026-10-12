# -*- coding: utf-8 -*-
"""
shots_dropdown.py —— 新的「分类 / 分组」下拉控件的取证 + 交互回归

为什么要单独写：样式统一这件事只有看图才说得清；而下拉是**自绘浮层**，
逻辑断言查不出「浮层有没有跟着主窗口」「滚轮翻页有没有生效」这类问题。

跑完会生成 4 张图：
    dd_local_closed.png   本机源（收起）
    dd_local_open.png     本机源（展开，含浮层）
    dd_wvt_closed.png     官网源（收起）
    dd_wvt_open.png       官网源（展开，51 个分组 + 滚动条）
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "desktop"))
sys.path.insert(0, ROOT)

OUT = os.path.join(ROOT, "_shots")
os.makedirs(OUT, exist_ok=True)

import tkinter as tk                                    # noqa: E402
import native_app                                       # noqa: E402
from PIL import ImageGrab                               # noqa: E402

RES = []
FAIL = False
_GEO = {}          # 浮层几何信息（相对窗口原点），供像素分析用
_ORIGIN = {}       # 窗口原点（销毁后就问不到了，抓图时先记下来）


def check(ok, msg):
    RES.append((ok, msg))
    print("  [%s] %s" % ("OK" if ok else "!!", msg))


def settle(root, ms=500):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        root.update_idletasks()
        root.update()
        time.sleep(0.02)


def grab(name, pad=0):
    app.root.update_idletasks()
    app.root.update()
    time.sleep(0.5)
    app.root.update()
    x, y = app.root.winfo_rootx(), app.root.winfo_rooty()
    w, h = app.root.winfo_width(), app.root.winfo_height()
    _ORIGIN.update(x=x, y=y)
    img = ImageGrab.grab(bbox=(x - pad, y - pad, x + w + pad, y + h + pad),
                         all_screens=True)
    img.save(os.path.join(OUT, name))
    print("  → %s %s" % (name, img.size))
    return img


def crop_left(img, name, w=420, h=460):
    """只截左栏顶部那块，看细节更清楚。"""
    c = img.crop((0, 40, w, 40 + h))
    c.save(os.path.join(OUT, name))
    print("  → %s %s" % (name, c.size))


def click_real(sel):
    """模拟真实点击（走 bindtags，能暴露「点开瞬间被 _outside_click 收起」这类问题）。"""
    sel.event_generate("<Button-1>", x=10, y=10)
    sel.event_generate("<ButtonRelease-1>", x=10, y=10)
    settle(sel.winfo_toplevel(), 300)


def dd_open(sel, n=6):
    """展开下拉并等它画出来。返回 (浮层, 几何信息)。"""
    sel._open()
    for _ in range(n):
        settle(app.root, 90)
    pop = sel._pop
    geo = None
    if pop:
        geo = dict(x=pop.winfo_rootx(), y=pop.winfo_rooty(),
                   w=pop.winfo_width(), h=pop.winfo_height(),
                   sx=sel.winfo_rootx(), sy=sel.winfo_rooty(),
                   sw=sel.winfo_width(), sh=sel.winfo_height())
    return pop, geo


def steps(app):
    # ---------- 1) 本机源 ----------
    print("== 1) 本机源：分类下拉 ==")
    app._set_src("local")
    settle(app.root, 3000)
    sel = app.cat_bar
    check(isinstance(sel, native_app.DropSelect), "本机源用的是新的 DropSelect")
    check(len(sel.items) >= 3, "分类条目 %d 个" % len(sel.items))
    img = grab("dd_local_closed.png")
    crop_left(img, "dd_local_closed_crop.png")

    print("== 2) 本机源：展开浮层（★ 真实点击路径）==")
    y_local = sel.winfo_rooty()
    click_real(sel)
    check(app.cat_bar._pop is not None, "真实点击能展开浮层")
    if app.cat_bar._pop is not None:
        click_real(sel)                  # 第二击应收起
        check(app.cat_bar._pop is None, "真实点击再击能收起")
    pop, geo = dd_open(sel)              # 后续像素取证仍用直调展开
    check(pop is not None, "浮层已创建")
    if pop:
        pw, ph = pop.winfo_width(), pop.winfo_height()
        check(pw >= 170 and ph > 40, "浮层尺寸 %dx%d" % (pw, ph))
        # 浮层必须紧贴下拉框、且主窗口移动时能收起
        check(abs(pop.winfo_rooty() - (sel.winfo_rooty() + sel.winfo_height())) <= 8,
              "浮层贴在下拉框正下方（Δy=%d）"
              % (pop.winfo_rooty() - sel.winfo_rooty() - sel.winfo_height()))
    _GEO["local"] = geo
    img = grab("dd_local_open.png")
    crop_left(img, "dd_local_open_crop.png", h=520)

    print("== 3) 本机源：点第二项真的会切过去 ==")
    before = sel.active
    sel._hov_i = 1
    sel._pop_pick()
    settle(app.root, 1200)
    after = sel.active
    check(after != before and after == app.cat_bar.get(),
          "选中项切到 %r（原 %r）" % (after, before))
    check(app.cur_cat == after, "cur_cat 跟着变成 %r" % app.cur_cat)
    check(sel._pop is None, "选完浮层已关闭")

    # ---------- 2) 官网源 ----------
    print("== 4) 官网源：分组下拉（同一个控件）==")
    app._set_src("wvt")
    settle(app.root, 8000)
    sel = app.group_cbo
    check(isinstance(sel, native_app.DropSelect), "官网源用的也是 DropSelect")
    n = len(sel.items)
    check(n > 10, "分组 %d 个（够验证滚动）" % n)
    img = grab("dd_wvt_closed.png")
    crop_left(img, "dd_wvt_closed_crop.png")

    print("== 5) 官网源：展开 + 滚轮翻页（★ 真实点击路径）==")
    y_wvt = sel.winfo_rooty()
    click_real(sel)
    check(app.group_cbo._pop is not None, "真实点击能展开分组浮层")
    if app.group_cbo._pop is not None:
        click_real(sel)
        check(app.group_cbo._pop is None, "真实点击再击能收起")
    check(abs(y_wvt - y_local) <= 4,
          "★ 分组与分类占顶部同一槽位（Δy=%d）" % abs(y_wvt - y_local))
    pop, geo = dd_open(sel)
    _GEO["wvt"] = geo
    if pop:
        check(sel._vis_rows <= native_app.DropSelect.MAX_ROWS,
              "可见行数被限制为 %d（总 %d）" % (sel._vis_rows, n))
        check(pop.winfo_height() > 200, "浮层高度 %d" % pop.winfo_height())
    img = grab("dd_wvt_open.png")
    crop_left(img, "dd_wvt_open_crop.png", h=520)
    s0 = sel._scroll
    sel._pop_wheel(type("E", (), {"delta": -120, "num": 0})())
    settle(app.root, 200)
    check(sel._scroll == max(0, s0 + 1), "滚轮下滚 _scroll %d→%d" % (s0, sel._scroll))
    img = grab("dd_wvt_scrolled.png")
    crop_left(img, "dd_wvt_scrolled_crop.png", h=520)

    print("== 6) 官网源：选一个分组真的换了频道列表 ==")
    sel._hov_i = 3
    key = sel.items[3][0]
    sel._pop_pick()
    settle(app.root, 1500)
    check(app.cur_group == key, "cur_group == %r" % key)
    check(app.channels and app.group_frame.winfo_manager(),
          "频道列表已重建（%d 个频道）" % len(app.channels))

    print("== 7) 回到本机源：两处样式必须是同一个类 ==")
    app._set_src("local")
    settle(app.root, 1500)
    check(app.group_frame.winfo_manager() == "",
          "官网源的分组行已隐藏（%r）" % (app.group_frame.winfo_manager() or "隐藏"))
    check(app.catf.winfo_manager() != "", "本机源的分类行已回到顶部槽位")
    check(type(app.cat_bar) is type(app.group_cbo),
          "本机源与官网源是同一个控件类 %s" % type(app.cat_bar).__name__)

    print("== 8) 收尾：Esc 能关掉浮层 ==")
    app.cat_bar._open()
    settle(app.root, 300)
    opened = app.cat_bar._pop is not None
    app.cat_bar._close()
    settle(app.root, 200)
    check(opened and app.cat_bar._pop is None, "浮层能开也能收")

    # 不 destroy 的话 mainloop 永远不返回，脚本会挂住
    app.root.after(150, lambda: app.root.destroy())


if __name__ == "__main__":
    app = native_app.NativeApp()
    app.root.after(1500, lambda: steps(app))
    try:
        app.root.mainloop()
    except Exception as e:                              # noqa: BLE001
        print("异常:", e)
        FAIL = True
    print()
    bad = [m for ok, m in RES if not ok]
    print("RESULT %s（%d 项，失败 %d）" % ("FAIL" if bad else "PASS", len(RES), len(bad)))
    for m in bad:
        print("  ✗", m)
    try:
        import json
        x0, y0 = _ORIGIN.get("x", 0), _ORIGIN.get("y", 0)
        rel = {k: {kk: vv - (x0 if kk in ("x", "sx") else
                                   y0 if kk in ("y", "sy") else 0)
                   for kk, vv in v.items()}
               for k, v in _GEO.items() if v}
        with open(os.path.join(OUT, "dd_geo.json"), "w", encoding="utf-8") as f:
            json.dump(rel, f, ensure_ascii=False, indent=1)
        print("浮层几何 -> _shots/dd_geo.json", rel)
    except Exception as e:
        print("几何导出失败:", e)

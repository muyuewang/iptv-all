# -*- coding: utf-8 -*-
"""
复现用户反馈：第一次点击下拉框后出现「竖排字」。
流程 A：启动 -> 本机源 -> 点「分类」下拉 -> 截图 -> 像素分析
流程 B：切官网源 -> 点「分组」下拉 -> 截图 -> 像素分析
只读窗口自身像素，不碰全屏。
"""
import os, sys, time, ctypes
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "desktop"))
os.chdir(os.path.join(os.path.dirname(__file__), "..", "desktop"))

try:
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass

import native_app as na

def shot_region(x, y, w, h, name):
    """GDI 抓屏指定区域 -> PIL Image"""
    from PIL import ImageGrab
    img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
    img.save(name)
    return img

def analyze(img, tag):
    """检测『竖排字』：某几列出现密集的亮色像素（文字），且列宽 < 12px 成串。
    正常横排文字的亮像素在每行内横向铺开；竖排字则集中在 1~2 列。"""
    g = img.convert("L")
    W, H = g.size
    px = g.load()
    # 每列的「文字像素」数（背景 ~ 20-40，文字 150+）
    colcnt = [0] * W
    for xx in range(W):
        c = 0
        for yy in range(H):
            if px[xx, yy] > 140:
                c += 1
        colcnt[xx] = c
    # 找出文字像素多的列，统计它们是否挤在 <14px 的窄带里且总量大
    hot = [xx for xx in range(W) if colcnt[xx] >= 8]
    total_hot = sum(colcnt[xx] for xx in hot)
    # 窄带：连续 hot 列组成的段，段宽 < 14px 但段内像素量大
    bands, s = [], None
    for xx in range(W + 1):
        h = xx < W and xx in set(hot)
        if h and s is None:
            s = xx
        elif not h and s is not None:
            bands.append((s, xx - 1)); s = None
    narrow = [b for b in bands if b[1] - b[0] <= 12]
    narrow_px = sum(colcnt[xx] for b in narrow for xx in range(b[0], b[1] + 1))
    print("[%s] hot列=%d 窄带数=%d 窄带像素=%d 总文字像素=%d"
          % (tag, len(hot), len(narrow), narrow_px, total_hot))
    vertical = narrow_px > 200 and (narrow_px / max(1, total_hot)) > 0.5
    print("[%s] 竖排字判定: %s" % (tag, "YES <<< 有问题" if vertical else "no"))
    return vertical

def click(widget):
    widget.update_idletasks()
    widget.event_generate("<Button-1>", x=10, y=10)
    widget.event_generate("<ButtonRelease-1>", x=10, y=10)
    widget.update()

def popup_geom(app):
    p = app.cat_bar._pop
    if p is None:
        return None
    return (p.winfo_rootx(), p.winfo_rooty(), p.winfo_width(), p.winfo_height())

def run_steps(app):
    """全部流程；由 mainloop 的 after 驱动（后台线程的 _ui/after 才能工作）。"""
    ry = app.root.winfo_rooty()
    ok = True

    def step1():
        nonlocal ok
        print("local: cat_bar y =", app.cat_bar.winfo_rooty() - ry,
              " 映射 =", bool(app.cat_bar.winfo_ismapped()),
              " group_frame =", app.group_frame.winfo_manager() or "隐藏")
        y_local = app.cat_bar.winfo_rooty() - ry

        # 流程 A：本机源「分类」真实点击 开→关
        click(app.cat_bar)
        app.root.after(500, lambda: step2(y_local))

    def step2(y_local):
        nonlocal ok
        g = popup_geom(app)
        opened = app.cat_bar._pop is not None
        print("A: popup =", g, " 开着 =", opened)
        bad = False
        if g and opened:
            img = shot_region(g[0], g[1], g[2], g[3], "_probe_dd_A.png")
            bad = analyze(img, "A 分类")
            if g[2] < 176:
                print("[A] 浮层宽度 %d < MIN_W 176 <<< 有问题" % g[2]); bad = True
        else:
            print("A: popup 未打开！"); bad = True
        ok = ok and not bad
        click(app.cat_bar)                      # 第二击应收起
        app.root.after(400, lambda: step3(y_local))

    def step3(y_local):
        nonlocal ok
        print("A: 第二击后收起 =", app.cat_bar._pop is None)
        app._set_src("wvt")
        wait_wvt(0)

    def wait_wvt(n):
        app.root.update()
        if not app.wvt_loaded and n < 160:
            app.root.after(100, lambda: wait_wvt(n + 1))
            return
        step4()

    def step4():
        nonlocal ok
        app.root.update_idletasks(); app.root.update()
        print("wvt: group_cbo y =", app.group_cbo.winfo_rooty() - ry,
              " 映射 =", bool(app.group_cbo.winfo_ismapped()),
              " catf =", app.catf.winfo_manager() or "隐藏",
              " 分组数 =", len(app.group_cbo.items))
        y_wvt = app.group_cbo.winfo_rooty() - ry
        same = abs(y_local_holder[0] - y_wvt) <= 4
        print("槽位统一(分类/分组同一位置):", "OK" if same else "FAIL Δ=%d" % (y_wvt - y_local_holder[0]))
        ok = ok and same
        click(app.group_cbo)
        app.root.after(500, step5)

    def step5():
        nonlocal ok
        p = app.group_cbo._pop
        opened = p is not None
        print("B: popup 开着 =", opened, end=" ")
        bad = False
        if opened:
            g = (p.winfo_rootx(), p.winfo_rooty(), p.winfo_width(), p.winfo_height())
            print("geom =", g)
            img = shot_region(g[0], g[1], g[2], g[3], "_probe_dd_B.png")
            bad = analyze(img, "B 分组")
            if g[2] < 176:
                print("[B] 浮层宽度 %d < MIN_W 176 <<< 有问题" % g[2]); bad = True
        else:
            print("<< 未打开！")
        ok = ok and not bad
        print("RESULT", "FAIL" if not ok else "PASS")
        app.root.destroy()

    y_local_holder = [0]

    def step0():
        app._set_src("local")
        app.root.after(1500, step1)
        app.root.after(1400, lambda: y_local_holder.__setitem__(
            0, app.cat_bar.winfo_rooty() - app.root.winfo_rooty()))

    app.root.after(300, step0)


def main():
    app = na.NativeApp()
    app.root.after(600, lambda: run_steps(app))
    app.root.mainloop()

if __name__ == "__main__":
    main()

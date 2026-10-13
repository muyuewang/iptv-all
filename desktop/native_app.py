# -*- coding: utf-8 -*-
"""
纯净电视直播 · 桌面原生一体化版（Windows / Linux）
================================================================
用 tkinter 建一个真正原生的窗口，mpv 画面内嵌其中（不再弹独立窗口），
频道列表 + 节目单 + 控制条都在同一个窗口里 —— 体验等同电视家等成熟直播 App。

架构：
  主窗口(tkinter)
  ├── 左侧：搜索框 + 分类标签 + 频道网格（带台标，可滚动）
  ├── 右侧：播放区（mpv --wid 内嵌）+ 底部节目单条
  └── 底部控制条：线路切换 / 播放暂停 / 音量 / 画面比例 / 收藏

硬解：mpv --hwdec=auto 走 D3D11VA/VAAPI GPU 硬解，CPU 几乎不参与。
控制：mpv --input-ipc-server 命名管道（Windows） / unix socket（Linux），
      Python 通过 JSON IPC 发命令（暂停 / 音量 / 画面比例）。
稳定性：mpv 异常退出自动切下一线路；全部线路失败则重新抓播放页刷新 token 重试。

依赖：Python(带 tkinter) + mpv（内置捆绑或系统安装）。业务逻辑来自 shared/core.py。
台标：assets/logos.js（1800+ 条）；装了 Pillow 走完整解码，没装则 PNG 台标原生解码。
"""
import os
import io
import sys
import json
import time
import queue
import socket
import hashlib
import colorsys
import shutil
import threading
import subprocess
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_MEIPASS = getattr(sys, "_MEIPASS", _HERE)

# 载入共享核心
import importlib.util as _ilu
_core_cands = [
    os.path.join(_MEIPASS, "shared", "core.py"),
    os.path.join(_HERE, "shared", "core.py"),
    os.path.join(_HERE, "..", "shared", "core.py"),
]
_core = None
for _p in _core_cands:
    if os.path.isfile(_p):
        spec = _ilu.spec_from_file_location("cleantv_core", _p)
        _core = _ilu.module_from_spec(spec)
        sys.modules["cleantv_core"] = _core
        spec.loader.exec_module(_core)
        break
if _core is None:
    raise ImportError("找不到 shared/core.py")


def _load_shared(name, modname):
    """按同样的策略载入 shared/ 下的另一个模块（wvt.py）。"""
    cands = [
        os.path.join(_MEIPASS, "shared", name),
        os.path.join(_HERE, "shared", name),
        os.path.join(_HERE, "..", "shared", name),
    ]
    for p in cands:
        if os.path.isfile(p):
            spec = _ilu.spec_from_file_location(modname, p)
            m = _ilu.module_from_spec(spec)
            sys.modules[modname] = m
            spec.loader.exec_module(m)
            return m
    return None


# WebViewTV 官网源适配层（解密 / 镜像回退 / 频道表）
_wvt = _load_shared("wvt.py", "cleantv_wvt")

# 官网源嗅探器（CDP）—— 没有 Edge/Chrome 时的退路
_cdp = None
try:
    import cdp as _cdp                      # desktop/cdp.py
except Exception as _e:                     # noqa: BLE001
    print("提示：官网源嗅探器不可用（%s）" % _e, file=sys.stderr)

# 官网源网页线路的内嵌浏览器（把 Edge 窗口嵌进播放区）
_embed_mod = None
try:
    import embed as _embed_mod              # desktop/embed.py
except Exception as _e:                     # noqa: BLE001
    print("提示：内嵌浏览器不可用（%s）" % _e, file=sys.stderr)

IS_WIN = sys.platform.startswith("win")


def _data_dir():
    """配置写入位置：打包后写 exe 所在目录（绿色便携），源码运行写脚本目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return _HERE


DATA_DIR = _data_dir()


def _find_asset(name):
    for base in (_MEIPASS, _HERE, os.path.join(_HERE, "..")):
        p = os.path.join(base, "assets", name)
        if os.path.isfile(p):
            return p
    return None


# ------------------------------------------------------------------ mpv 探测
def find_mpv():
    base = _MEIPASS
    bundled = os.path.join(base, "mpv", "mpv.exe" if IS_WIN else "mpv")
    if os.path.isfile(bundled):
        return bundled
    p = shutil.which("mpv")
    if p:
        return p
    if IS_WIN:
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        for c in (os.path.join(pf, "MPV Player", "mpv.exe"),
                  os.path.join(pf86, "MPV Player", "mpv.exe"),
                  os.path.join(pf, "mpv", "mpv.exe")):
            if os.path.isfile(c):
                return c
    else:
        for c in ("/usr/bin/mpv", "/usr/local/bin/mpv"):
            if os.path.isfile(c):
                return c
    return None


# ------------------------------------------------------------------ GUI
try:
    import tkinter as tk
    from tkinter import ttk
except Exception:
    print("缺少 tkinter：请用系统 Python 运行（Windows 官方 Python 自带）", file=sys.stderr)
    raise

# 设计令牌（与 web 端 :root 对齐）
BG = "#0a0b0f"
BG2 = "#14161d"
BG3 = "#1c1f28"
FG = "#f2f4f8"
FG_DIM = "#a2a9b8"
FG_MUTE = "#6b7385"
LINE = "#252833"
BRAND = "#ff2d55"
BRAND2 = "#ff6b35"
SEL_BG = "#2b171c"              # 频道列表选中行底色（品牌红的暗调）
GOLD = "#ffc531"

# 台标缩略尺寸
LOGO_SIZE = 30

# 官网源分组胶囊区高度（4 行 × 30px + 3 × 6px 间距）—— 51 个分组放不下，
# 限高后内部滚动；前 12 个分组（广东/四川/河南/山东/卫视/江苏…）始终可见。
GROUP_BAR_H = 138


def badge_color(name):
    """按频道名生成稳定的徽标底色（无台标时用）。"""
    h = 0
    for ch in (name or "?"):
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    hue = (h % 360) / 360.0
    r, g, b = colorsys.hsv_to_rgb(hue, 0.52, 0.70)
    return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))


# ------------------------------------------------------------------ 胶囊标签条
def _hex_rgb(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _lerp_rgb(a, b, t):
    return "#%02x%02x%02x" % tuple(
        int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


class PillBar(tk.Canvas):
    """
    一排圆角胶囊按钮（Canvas 手绘）。

    顶栏的「本机源 / 官网源」和左侧的分类标签**共用同一个控件**，所以两处的
    圆角半径、高度、间距、字体、选中渐变完全一致 —— 之前一个是 Button 包在
    深色 Frame 里的"分段控件"，另一个是三列平铺的原生 Button，风格割裂。

    为什么手绘：ttk/tk Button 做不出圆角 + 渐变，且外观随 Windows 主题变化；
    Canvas 画出来的在哪个系统上都长一样。
    """

    def __init__(self, master, cols=3, cell_h=32, gap=6, radius=9, font=None,
                 on_pick=None, pad=0, **kw):
        kw.setdefault("width", 10)
        super().__init__(master, bg=BG, highlightthickness=0, bd=0, **kw)
        self.cols = max(1, cols)
        self.cell_h = cell_h
        self.gap = gap
        self.radius = radius
        self.font = font or ("TkDefaultFont", 10)
        self.on_pick = on_pick
        self.pad = pad
        self.items = []
        self.active = None
        self._hover = None
        self._zones = []
        self.bind("<Button-1>", self._on_click)
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Configure>", lambda e: self.redraw())

    # ---------------- 数据 ----------------
    def set_items(self, items, active=None):
        self.items = list(items or [])
        if active is not None:
            self.active = active
        self.redraw()

    def set_active(self, key):
        if key == self.active:
            return
        self.active = key
        self.redraw()

    # ---------------- 绘制 ----------------
    def _font(self, bold):
        fam, size = self.font[0], self.font[1]
        return (fam, size, "bold") if bold else (fam, size)

    def _rows(self):
        return max(1, (len(self.items) + self.cols - 1) // self.cols)

    def _round(self, x0, y0, x1, y1, r, fill):
        r = max(0.0, min(r, (x1 - x0) / 2.0, (y1 - y0) / 2.0))
        self.create_polygon(
            x0 + r, y0, x1 - r, y0, x1 - r, y0, x1, y0,
            x1, y0 + r, x1, y0 + r, x1, y1 - r, x1, y1 - r,
            x1, y1, x1 - r, y1, x1 - r, y1, x0 + r, y1,
            x0 + r, y1, x0, y1, x0, y1 - r, x0, y1 - r,
            x0, y0 + r, x0, y0 + r, x0 + r, y0, x0 + r, y0,
            fill=fill, outline="", smooth=True)

    def _grad_round(self, x0, y0, x1, y1, r, c_top, c_bot):
        """圆角矩形 + 垂直渐变（逐行画线，按圆角几何算每行的左右内缩）。"""
        x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
        h = max(1, y1 - y0)
        r = max(1, min(r, (x1 - x0) // 2, h // 2))
        a, b = _hex_rgb(c_top), _hex_rgb(c_bot)
        for i in range(h):
            yy = y0 + i + 0.5
            t = i / float(h - 1) if h > 1 else 0.0
            if i < r:
                d = r - i
                inset = r - (r * r - d * d) ** 0.5
            elif i > h - r:
                d = r - (h - i)
                inset = r - (r * r - d * d) ** 0.5
            else:
                inset = 0.0
            self.create_line(x0 + inset, yy, x1 - inset, yy,
                             fill=_lerp_rgb(a, b, t))

    def redraw(self):
        self.delete("all")
        self._zones = []
        n = len(self.items)
        rows = self._rows()
        need_h = rows * self.cell_h + (rows - 1) * self.gap + self.pad * 2
        try:
            if int(float(self.cget("height"))) != int(need_h):
                self.configure(height=need_h)
        except Exception:
            pass
        if not n:
            return
        W = self.winfo_width()
        if W <= 1:
            W = 300
        cell_w = (W - self.pad * 2 - (self.cols - 1) * self.gap) / float(self.cols)
        for i, (key, label) in enumerate(self.items):
            r, c = divmod(i, self.cols)
            x0 = self.pad + c * (cell_w + self.gap)
            y0 = self.pad + r * (self.cell_h + self.gap)
            x1, y1 = x0 + cell_w, y0 + self.cell_h
            self._zones.append((key, x0, y0, x1, y1))
            if key == self.active:
                self._grad_round(x0, y0, x1, y1, self.radius, BRAND2, BRAND)
                fg, bold = "#ffffff", True
            else:
                hov = (key == self._hover)
                self._round(x0, y0, x1, y1, self.radius, BG3 if hov else BG2)
                fg, bold = (FG if hov else FG_DIM), False
            self.create_text((x0 + x1) / 2.0, (y0 + y1) / 2.0 + 1,
                             text=label, fill=fg, font=self._font(bold))

    # ---------------- 交互 ----------------
    def _hit(self, x, y):
        for key, x0, y0, x1, y1 in self._zones:
            if x0 <= x <= x1 and y0 <= y <= y1:
                return key
        return None

    def _on_click(self, e):
        k = self._hit(e.x, e.y)
        if k is not None and self.on_pick:
            self.on_pick(k)

    def _on_motion(self, e):
        k = self._hit(e.x, e.y)
        if k != self._hover:
            self._hover = k
            self.redraw()

    def _on_leave(self, e):
        if self._hover is not None:
            self._hover = None
            self.redraw()



class DropSelect(tk.Canvas):
    """
    手绘的**美化下拉框**（取代原生 ttk.Combobox）。

    本机源的「分类」和官网源的「分组」共用同一个控件 —— 之前一个是三列平铺的
    胶囊条、一个是原生 Combobox，两处点下去长得完全不一样；原生 Combobox 在
    深色主题下是「系统灰 + 方角 + 蓝焦点框」，跟整套暗色 UI 根本不搭。

    为什么自己画：
      · `ttk.Style` 只管得住输入框那一块，**弹出的列表是系统绘制的**，改不动
        （这才是「很丑」的真正来源）。
      · Canvas 手绘 + `overrideredirect` 浮层，圆角 / 品牌渐变 / 悬停高亮 /
        选中标记全部自绘，配色与 PillBar 完全一致。

    浮层关闭时机：选中某项 / Esc / 点到别处 / 主窗口移动缩放 —— 都会收起，
    不会出现「列表贴在屏幕上不消失」。
    """

    ROW_H = 32              # 列表每行高
    PAD = 6                 # 面板内边距
    MAX_ROWS = 10           # 超过就面板内部滚动（官网源有 51 个分组）
    MIN_W = 176             # 面板最小宽度

    def __init__(self, master, items=None, active=None, height=36, radius=10,
                 font=None, on_pick=None, placeholder="请选择", **kw):
        kw.setdefault("width", 10)
        super().__init__(master, bg=BG, highlightthickness=0, bd=0,
                         height=height, cursor="hand2", **kw)
        self.items = list(items or [])          # [(key, label), ...]
        self.active = active
        self.h = height
        self.radius = radius
        self.font = font or ("TkDefaultFont", 10)
        self.on_pick = on_pick
        self.placeholder = placeholder
        self._hover = False
        self._pop = None
        self._pop_cv = None
        self._rows = []            # 浮层命中区 [(index, x0, y0, x1, y1)]
        self._hov_i = None
        self._scroll = 0           # 已滚掉的行数
        self._vis_rows = 0
        self._root_bind = None
        self.bind("<Button-1>", lambda e: self._toggle())
        self.bind("<Motion>", self._hover_on)
        self.bind("<Leave>", self._hover_off)
        self.bind("<Configure>", lambda e: self.redraw())

    # ---------------- 数据 ----------------
    def set_items(self, items, active=None):
        self.items = list(items or [])
        if active is not None:
            self.active = active
        keys = [k for k, _l in self.items]
        if keys and self.active not in keys:
            self.active = keys[0]
        elif not keys:
            self.active = None
        self.redraw()

    def set_active(self, key):
        self.active = key
        self.redraw()

    # Combobox 兼容：`cb.configure(values=...)` / `cb.cget("values")`
    def set(self, key):
        self.set_active(key)

    def get(self):
        return self.active

    def cget(self, key):
        if key == "values":
            return [k for k, _l in self.items]
        return super().cget(key)

    def configure(self, cnf=None, **kw):
        vals = None
        if kw and "values" in kw:
            vals = kw.pop("values")
        elif cnf and isinstance(cnf, dict) and "values" in cnf:
            vals = cnf["values"]
            cnf = {k: v for k, v in cnf.items() if k != "values"}
        if vals is not None:
            self.set_items([(v, v) for v in vals])
        if cnf or kw:
            return super().configure(cnf, **kw)
        return super().configure()

    config = configure

    def _label(self):
        for k, l in self.items:
            if k == self.active:
                return l
        return self.placeholder

    # ---------------- 绘制基础 ----------------
    @staticmethod
    def _round(cv, x0, y0, x1, y1, r, fill, outline=""):
        r = max(0.0, min(r, (x1 - x0) / 2.0, (y1 - y0) / 2.0))
        cv.create_polygon(
            x0 + r, y0, x1 - r, y0, x1 - r, y0, x1, y0,
            x1, y0 + r, x1, y0 + r, x1, y1 - r, x1, y1 - r,
            x1, y1, x1 - r, y1, x1 - r, y1, x0 + r, y1,
            x0 + r, y1, x0, y1, x0, y1 - r, x0, y1 - r,
            x0, y0 + r, x0, y0 + r, x0 + r, y0, x0 + r, y0,
            fill=fill, outline=outline, smooth=True)

    @staticmethod
    def _grad_round(cv, x0, y0, x1, y1, r, c_top, c_bot):
        """圆角矩形 + 垂直渐变（逐行画线，按圆角几何算每行左右内缩）。"""
        x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
        h = max(1, y1 - y0)
        r = max(1, min(r, (x1 - x0) // 2, h // 2))
        a, b = _hex_rgb(c_top), _hex_rgb(c_bot)
        for i in range(h):
            yy = y0 + i + 0.5
            t = i / float(h - 1) if h > 1 else 0.0
            if i < r:
                d = r - i
                inset = r - (r * r - d * d) ** 0.5
            elif i > h - r:
                d = r - (h - i)
                inset = r - (r * r - d * d) ** 0.5
            else:
                inset = 0.0
            cv.create_line(x0 + inset, yy, x1 - inset, yy, fill=_lerp_rgb(a, b, t))

    # ---------------- 绘制：收起态 ----------------
    def redraw(self):
        self.delete("all")
        W = self.winfo_width() or 240
        H = self.winfo_height() or self.h
        if W <= 1:
            W = 240
        x0, y0, x1, y1 = 1, 1, W - 1, H - 1
        open_ = self._pop is not None
        edge = BRAND2 if (self._hover or open_) else LINE
        self._round(self, x0, y0, x1, y1, self.radius, "", edge)
        if self._hover or open_:
            self._round(self, x0 + 1, y0 + 1, x1 - 1, y1 - 1, self.radius - 1,
                        "#262a36" if open_ else "#20242f")
        else:
            self._grad_round(self, x0 + 1, y0 + 1, x1 - 1, y1 - 1, self.radius - 1,
                             "#20242e", "#191c25")
        # 左侧品牌色竖条
        self._grad_round(self, x0 + 10, y0 + 9, x0 + 13, y1 - 9, 1.5, BRAND2, BRAND)
        # 文字
        self.create_text(x0 + 24, (y0 + y1) / 2.0, anchor="w",
                         text=self._label(),
                         fill=FG if self.active else FG_MUTE,
                         font=self.font, width=max(10, W - 62))
        # 箭头（画三角形，不用字体符号 —— 字体符号深浅不一还可能缺字）
        cx, cy = W - 18, (y0 + y1) / 2.0
        tcol = BRAND2 if open_ else FG_MUTE
        if open_:
            self.create_polygon(cx - 5, cy - 2, cx + 5, cy - 2, cx, cy + 4,
                                fill=tcol, outline="")
        else:
            self.create_polygon(cx - 5, cy - 2, cx + 5, cy - 2, cx, cy + 4,
                                fill=tcol, outline="")

    def _hover_on(self, e):
        if not self._hover:
            self._hover = True
            self.redraw()

    def _hover_off(self, e):
        if self._hover:
            self._hover = False
            self.redraw()

    # ---------------- 展开 / 收起 ----------------
    def _toggle(self):
        self._close() if self._pop is not None else self._open()

    def _open(self):
        if not self.items:
            return
        self.update_idletasks()
        n = len(self.items)
        self._scroll = 0
        self._hov_i = None
        self._vis_rows = min(n, self.MAX_ROWS)
        panel_h = self._vis_rows * self.ROW_H + self.PAD * 2
        try:
            import tkinter.font as tkfont
            f = tkfont.Font(font=self.font)
            tw = max(f.measure(l or "") for _k, l in self.items)
        except Exception:
            tw = max(len(l or "") for _k, l in self.items) * 12
        panel_w = max(self.MIN_W, self.winfo_width(), tw + 56)
        sh = self.winfo_screenheight()
        if panel_h > sh - 16:                    # 屏幕太矮，砍可见行数
            panel_h = sh - 16
            self._vis_rows = max(1, (panel_h - self.PAD * 2) // self.ROW_H)

        root = self.winfo_toplevel()
        pop = tk.Toplevel(root)
        pop.overrideredirect(True)
        pop.configure(bg=BG)
        try:
            pop.attributes("-topmost", True)
        except Exception:
            pass
        cv = tk.Canvas(pop, bg=BG, highlightthickness=0, bd=0,
                       width=panel_w, height=panel_h)
        cv.pack()
        self._pop, self._pop_cv = pop, cv
        # ★ 尺寸必须自己记住：_draw_pop 在窗口还没完成布局时就要画，
        #   那会儿 cv.winfo_width() 还是 1，照它画整条列表会挤成一竖条。
        self._pop_w, self._pop_h = panel_w, panel_h

        # 位置：贴着按钮左边缘往下，放不下就翻到上面
        bx, by = self.winfo_rootx(), self.winfo_rooty()
        bh = self.winfo_height() or self.h
        top = by + bh + 4
        if top + panel_h > sh - 8:
            top = max(8, by - panel_h - 4)
        left = min(max(8, bx), max(8, self.winfo_screenwidth() - panel_w - 8))
        pop.geometry("%dx%d+%d+%d" % (panel_w, panel_h, left, top))

        cv.bind("<Motion>", self._pop_motion)
        cv.bind("<Button-1>", self._pop_click)
        cv.bind("<MouseWheel>", self._pop_wheel)
        cv.bind("<Button-4>", self._pop_wheel)
        cv.bind("<Button-5>", self._pop_wheel)
        cv.bind("<Leave>", lambda e: self._pop_hover(None))
        pop.bind("<Escape>", lambda e: self._close())
        pop.bind("<Key-Up>", lambda e: self._pop_move(-1))
        pop.bind("<Key-Down>", lambda e: self._pop_move(1))
        pop.bind("<Key-Return>", lambda e: self._pop_pick())
        pop.bind("<FocusOut>", lambda e: self._close())
        try:
            pop.focus_force()
        except Exception:
            pass
        # 点别处收起 / 主窗口被拖动缩放收起
        self._root_bind = root.bind("<Button-1>", self._outside_click, add="+")
        root.bind("<Configure>", self._root_moved, add="+")
        self.redraw()
        self._draw_pop()

    def _close(self):
        if self._pop is None:
            return
        pop = self._pop
        try:
            self.winfo_toplevel().unbind("<Button-1>", self._root_bind)
        except Exception:
            pass
        try:
            self.winfo_toplevel().unbind("<Configure>")
        except Exception:
            pass
        self._root_bind = None
        self._pop = self._pop_cv = None
        self._rows = []
        try:
            pop.destroy()
        except Exception:
            pass
        self.redraw()

    def _root_moved(self, e):
        # 主窗口被拖动/缩放时浮层留在原地就成了「贴纸」，直接收起
        if self._pop is not None:
            self._close()
        # WebView2 需要被告知父窗口挪动了（同步它的弹层/下拉位置）。
        # ★ 本方法同时被 NativeApp 和 DropSelect 绑定到 root <Configure>，
        #   DropSelect 没有 embed 属性 —— 必须判空，否则每次挪窗口都抛异常。
        emb = getattr(self, "embed", None)
        if emb is not None:
            try:
                emb.notify_parent_moved()
            except Exception:
                pass

    def _outside_click(self, e):
        if self._pop is None:
            return
        # 点在下拉框按钮本身上：交给 _toggle 处理，不算「点别处」——
        # 按钮在主窗口里，这击也会冒泡到 root 的 <Button-1> 绑定，
        # 不拦的话「点开」和「点别处收起」会在同一次点击里打架。
        try:
            bx, by = self.winfo_rootx(), self.winfo_rooty()
            bw, bh = self.winfo_width(), self.winfo_height()
            if (bx - 2 <= e.x_root <= bx + bw + 2
                    and by - 2 <= e.y_root <= by + bh + 2):
                return
        except Exception:
            pass
        try:
            px, py = self._pop.winfo_rootx(), self._pop.winfo_rooty()
            pw, ph = self._pop.winfo_width(), self._pop.winfo_height()
            if px <= e.x_root <= px + pw and py <= e.y_root <= py + ph:
                return
        except Exception:
            return
        self._close()

    # ---------------- 绘制：浮层 ----------------
    def _draw_pop(self):
        cv = self._pop_cv
        if cv is None:
            return
        cv.delete("all")
        W = getattr(self, "_pop_w", 0) or cv.winfo_width() or 1
        H = getattr(self, "_pop_h", 0) or cv.winfo_height() or 1
        # ★ 硬兜底：宽度绝不允许小于 MIN_W —— 以前布局前取 winfo_width()=1，
        #   整条列表被挤成一竖条（一个字一个字竖着排），就是这里没拦住。
        if W < self.MIN_W:
            W = self.MIN_W
            self._pop_w = W
        if H < self.ROW_H + self.PAD * 2:
            H = self._vis_rows * self.ROW_H + self.PAD * 2
            self._pop_h = H
        self._round(cv, 1, 1, W - 1, H - 1, self.radius, BG2, LINE)
        self._rows = []
        x0, x1 = self.PAD + 8, W - self.PAD - 8
        y0 = self.PAD
        total = len(self.items)
        for i in range(self._vis_rows):
            n = self._scroll + i
            if n >= total:
                break
            key, label = self.items[n]
            yy = y0 + i * self.ROW_H
            y1 = yy + self.ROW_H - 2
            self._rows.append((n, x0, yy, x1, y1))
            hot = (n == self._hov_i)
            sel = (key == self.active)
            if hot:
                self._grad_round(cv, x0 - 4, yy, x1 + 4, y1, 7, BRAND2, BRAND)
                fg, bold = "#ffffff", True
            else:
                if sel:
                    self._round(cv, x0 - 4, yy, x1 + 4, y1, 7, "#232733")
                fg, bold = (FG if sel else FG_DIM), bool(sel)
            if sel:                              # 选中标记：左侧品牌渐变竖条
                self._grad_round(cv, x0 - 4, yy + 6, x0 - 1, y1 - 5, 1.5,
                                 BRAND2, BRAND)
            cv.create_text(x0 + 12, (yy + y1) / 2.0, anchor="w", text=label,
                           fill=fg, width=max(10, (x1 - x0) - 34),
                           font=(self.font[0], self.font[1],
                                 "bold" if bold else "normal"))
            if sel and not hot:
                cv.create_text(x1 - 2, (yy + y1) / 2.0, anchor="e", text="✓",
                               fill=BRAND2, font=self.font)
        if total > self._vis_rows:                # 极简滚动条 + 进度
            track = H - self.PAD * 2 - 4
            th = max(20, track * self._vis_rows // total)
            ty = (self.PAD + 2 + (track - th) * self._scroll
                  // max(1, total - self._vis_rows))
            self._round(cv, W - 5, ty, W - 2, ty + th, 1.5, BRAND2)
            cv.create_text(W / 2.0, H - 2, anchor="s",
                           text="滚轮翻页  %d/%d"
                                % (min(total, self._scroll + self._vis_rows), total),
                           fill=FG_MUTE,
                           font=(self.font[0], max(7, self.font[1] - 2)))

    # ---------------- 浮层交互 ----------------
    def _pop_hit(self, x, y):
        for i, rx0, ry0, rx1, ry1 in self._rows:
            if rx0 - 10 <= x <= rx1 + 10 and ry0 <= y <= ry1:
                return i
        return None

    def _pop_hover(self, i):
        if i == self._hov_i:
            return
        self._hov_i = i
        self._draw_pop()

    def _pop_motion(self, e):
        self._pop_hover(self._pop_hit(e.x, e.y))

    def _pop_click(self, e):
        i = self._pop_hit(e.x, e.y)
        if i is not None:
            self._hov_i = i
            self._pop_pick()

    def _pop_move(self, d):
        if not self.items:
            return
        cur = (self._hov_i if self._hov_i is not None else -1) + d
        cur = max(0, min(len(self.items) - 1, cur))
        if cur < self._scroll:
            self._scroll = cur
        elif cur >= self._scroll + self._vis_rows:
            self._scroll = cur - self._vis_rows + 1
        self._hov_i = cur
        self._draw_pop()

    def _pop_pick(self):
        i = self._hov_i
        if i is None or i >= len(self.items):
            return
        key = self.items[i][0]
        self._close()
        self.active = key
        if self.on_pick:
            self.on_pick(key)

    def _pop_wheel(self, e):
        if self._pop is None or len(self.items) <= self._vis_rows:
            return "break"
        d = getattr(e, "delta", 0)
        if not d:
            d = 120 if getattr(e, "num", 0) == 4 else -120
        self._scroll = max(0, min(len(self.items) - self._vis_rows,
                                  self._scroll + (-1 if d > 0 else 1)))
        self._draw_pop()
        return "break"          # 别让滚轮事件再透传给频道列表




# ------------------------------------------------------------------ 台标加载
class LogoStore:
    """后台下载台标 + 磁盘缓存；主线程创建 PhotoImage（Tk 不允许跨线程调图形）。"""

    def __init__(self, root, cache_dir, size=LOGO_SIZE):
        self.root = root
        self.size = size
        self.cache_dir = cache_dir
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except Exception:
            pass
        self.mem = {}                 # url -> PhotoImage | None
        self._sem = threading.Semaphore(6)
        try:
            from PIL import Image  # noqa: F401
            self.pil = True
        except Exception:
            self.pil = False

    def request(self, url, cb):
        """cb(photo_or_None) 会在主线程被调用。"""
        if not url:
            cb(None)
            return
        if url in self.mem:
            cb(self.mem[url])
            return

        def work():
            img = None
            with self._sem:
                try:
                    img = self._fetch(url)
                except Exception:
                    img = None
            self.root.after(0, lambda: self._finish(url, img, cb))

        threading.Thread(target=work, daemon=True).start()

    def _cache_path(self, url):
        return os.path.join(self.cache_dir, hashlib.md5(url.encode()).hexdigest() + ".png")

    def _fetch(self, url):
        cp = self._cache_path(url)
        if os.path.isfile(cp):
            return cp
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": _core.UA})
        data = urllib.request.urlopen(req, timeout=6).read()
        if not data:
            return None
        if self.pil:
            from PIL import Image
            im = Image.open(io.BytesIO(data)).convert("RGBA")
            im.thumbnail((self.size, self.size))
            im.save(cp)
            return cp
        # 无 Pillow：仅原生 PNG 可解
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            with open(cp, "wb") as f:
                f.write(data)
            return cp
        return None

    def _to_photo(self, path):
        try:
            img = tk.PhotoImage(file=path)
            k = max(1, (max(img.width(), img.height()) + self.size - 1) // self.size)
            if k > 1:
                img = img.subsample(k, k)
            return img
        except Exception:
            return None

    def _finish(self, url, path, cb):
        img = self._to_photo(path) if path else None
        self.mem[url] = img
        cb(img)


# ------------------------------------------------------------------ mpv 控制（IPC）
class MpvIPC:
    """通过 mpv 的 --input-ipc-server 发 JSON 命令（暂停 / 音量 / 画面比例）。"""

    def __init__(self, path):
        self.path = path
        self.conn = None
        self.lock = threading.Lock()

    def connect(self, timeout=6.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                if IS_WIN:
                    self.conn = open(self.path, "r+b", buffering=0)
                else:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.connect(self.path)
                    self.conn = s
                return True
            except Exception:
                time.sleep(0.15)
        return False

    def command(self, *args):
        if self.conn is None:
            return False
        payload = (json.dumps({"command": list(args)}) + "\n").encode()
        with self.lock:
            try:
                if IS_WIN:
                    self.conn.write(payload)
                else:
                    self.conn.sendall(payload)
                return True
            except Exception:
                return False

    def close(self):
        with self.lock:
            try:
                if self.conn:
                    self.conn.close()
            except Exception:
                pass
            self.conn = None


# ------------------------------------------------------------------ 主应用
class NativeApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("纯净电视直播")
        self.root.configure(bg=BG)
        self.root.geometry("1280x780")
        self.root.minsize(1000, 620)
        # 窗口/任务栏图标 —— 不设的话 Windows 显示 Tk 默认羽毛图标。
        # ★ iconbitmap 只可靠刷新标题栏小图标，任务栏大图标部分 Tk 版本不更新
        #   （v2.4.4 实测：标题栏换了、任务栏还是羽毛）→ 再补一发 WM_SETICON。
        ico = os.path.join(_MEIPASS, "app.ico")
        try:
            self.root.iconbitmap(ico)
        except Exception:
            pass
        self._after_map(self._set_taskbar_icon, (ico,))

        # 跨线程 UI 队列（_ui 线程安全依赖；先建好，后台线程随时可能调 _ui）
        self._ui_q = queue.Queue()
        self._closing = False

        self.font_family = self._pick_font()
        self._setup_style()

        self.mpv = find_mpv()
        self.mpv_proc = None
        self.ipc = None
        self._mpv_gen = 0
        self._paused = False
        self._volume = 100
        self._muted = False
        self._ratio = "适应"
        self._fullscreen = False

        # 设置
        self.settings_file = os.path.join(DATA_DIR, "settings.json")
        self.settings = _core.load_settings(self.settings_file)
        _core.apply_settings(self.settings)

        # 状态
        self.cur = {"tid": None, "id": None, "name": None, "urls": [], "idx": 0,
                    "epg": [], "src": "local", "extra": []}
        self.cats = list(_core.CATS)
        self.cur_cat = "tv"
        self.channels = []
        self.filtered = []
        self.search_kw = ""
        self.fav_file = os.path.join(DATA_DIR, "fav.json")
        self.fav = _core.load_fav(self.fav_file)
        self.mem_cache = {}
        self._try_round = 0
        self._refreshed = False
        self._rows = {}                 # (tid, id) -> (row, name)（选中高亮用）
        self._sel_key = None            # 当前选中频道的 (tid, id)

        # ---- 数据源：local=本机直播源 / wvt=官网源(WebViewTV) ----
        self.src_mode = "local"
        self.wvt_channels = []          # 官网源频道表
        self.wvt_by_id = {}             # id -> 频道（官网源）
        self.wvt_groups = []            # 分组名列表
        self.wvt_adapters = []          # 网页适配规则（注入脚本用）
        self.cur_group = None
        self.wvt_loaded = False
        self.sniff_dir = os.path.join(DATA_DIR, ".sniff")
        self._req = 0                   # 请求代号（防止过期嗅探结果覆盖新选择）
        self._sniff_note = ""

        # ---- 内嵌浏览器（官网源网页线路的播放器）----
        self.embed = None
        self._inject_src = None
        self._embed_seq = 0             # 换台代号：晚到的状态回调一律丢弃
        self._web_state = ""
        self._web_hint = ""

        # ---- 等待反馈（点击频道 → 起播之间的过渡动画；控件在 video_host 建好后创建）----
        self._load_base = "正在连接…"
        self._load_spin = 0
        self._load_bar_w = None
        self._load_bar_rect = None
        self._load_after = None
        self._load_t0 = 0.0

        # ---- 全屏 ----
        self._fs_overlay = None
        self._fs_btn = None
        self._fit_job = None
        self._fs_saved_epg = False
        self._hotkey_prev = (0, 0)

        # 台标
        self.logos = {}
        lp = _find_asset("logos.js")
        if lp:
            try:
                with open(lp, "r", encoding="utf-8") as f:
                    self.logos = _core.parse_logos(f.read())
            except Exception:
                self.logos = {}
        self.logo_store = LogoStore(self.root, os.path.join(DATA_DIR, "logocache"))

        self._build_ui()
        self._bind_keys()
        self._set_src("wvt")            # 默认官网源（官网源在第一位，v2.4.2）

        # 启动跨线程 UI 队列排水器（必须在主线程启动）
        self.root.after(30, self._drain_ui)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- 字体 / 主题 ----------------
    def _after_map(self, fn, args=()):
        """等主窗口完成映射（拿得到顶层 HWND）后执行 fn(*args)。"""
        def _try():
            try:
                if self.root.winfo_ismapped():
                    fn(*args)
                    return
            except Exception:
                pass
            self.root.after(100, _try)
        self.root.after(100, _try)

    def _set_taskbar_icon(self, ico_path):
        """WM_SETICON 大/小图标 —— 任务栏按钮取**大**图标，iconbitmap 只刷标题栏。"""
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(self.root.winfo_id())
            if not hwnd:                    # 顶层 HWND 还没建好，稍后再试
                self.root.after(300, self._set_taskbar_icon, ico_path)
                return
            IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10
            WM_SETICON, ICON_BIG, ICON_SMALL = 0x80, 1, 0
            big = user32.LoadImageW(None, ico_path, IMAGE_ICON,
                                    user32.GetSystemMetrics(11),    # SM_CXICON
                                    user32.GetSystemMetrics(12),    # SM_CYICON
                                    LR_LOADFROMFILE)
            small = user32.LoadImageW(None, ico_path, IMAGE_ICON,
                                      user32.GetSystemMetrics(49),  # SM_CXSMICON
                                      user32.GetSystemMetrics(50),  # SM_CYSMICON
                                      LR_LOADFROMFILE)
            if big:
                user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big)
            if small:
                user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small)
        except Exception:
            pass

    def _pick_font(self):
        try:
            import tkinter.font as tkfont
            fams = set(tkfont.families())
        except Exception:
            fams = set()
        for f in ("Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC",
                  "Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Micro Hei",
                  "DejaVu Sans"):
            if f in fams:
                return f
        return "TkDefaultFont"

    def F(self, size, bold=False):
        return (self.font_family, size, "bold") if bold else (self.font_family, size)

    def _setup_style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure("Dark.Vertical.TScrollbar", background=BG3, troughcolor=BG,
                     bordercolor=BG, arrowcolor=FG_DIM, darkcolor=BG3, lightcolor=BG3)
        st.configure("Dark.Horizontal.TScrollbar", background=BG3, troughcolor=BG,
                     bordercolor=BG, arrowcolor=FG_DIM, darkcolor=BG3, lightcolor=BG3)
        st.configure("Dark.TCombobox", fieldbackground=BG3, background=BG3,
                     foreground=FG, arrowcolor=FG_DIM, bordercolor=LINE)
        st.map("Dark.TCombobox", fieldbackground=[("readonly", BG3)],
               foreground=[("readonly", FG)])
        st.configure("Dark.TRadiobutton", background=BG2, foreground=FG)
        st.map("Dark.TRadiobutton", background=[("active", BG2)])

    # ---------------- UI 构建 ----------------
    def _build_ui(self):
        # 顶栏
        top = self.top_bar = tk.Frame(self.root, bg=BG, height=58)
        top.pack(fill="x", side="top")
        top.pack_propagate(False)
        tk.Label(top, text="● ", bg=BG, fg=BRAND,
                 font=self.F(12, True)).pack(side="left", padx=(16, 0), pady=14)
        tk.Label(top, text="纯净电视直播", bg=BG, fg=FG,
                 font=self.F(14, True)).pack(side="left", pady=14)

        # 数据源切换：官网源 / 本机源（官网源放第一位，v2.4.2 用户指定）
        self.src_bar = PillBar(top, cols=2, cell_h=30, gap=4, radius=8,
                               font=self.F(10), on_pick=self._set_src, width=178)
        self.src_bar.pack(side="left", padx=(20, 0), pady=14)
        self.src_bar.set_items([("wvt", "官网源"), ("local", "本机源")], "local")

        self.status_lbl = tk.Label(top, text="", bg=BG, fg=FG_MUTE, font=self.F(9))
        self.status_lbl.pack(side="right", padx=(8, 16))

        self.settings_btn = tk.Button(top, text="⚙ 设置", bg=BG2, fg=FG_DIM,
                                      relief="flat", bd=0, padx=12, pady=6,
                                      activebackground=BRAND2, activeforeground="#fff",
                                      font=self.F(10), cursor="hand2",
                                      command=self._open_settings)
        self.settings_btn.pack(side="right", padx=6, pady=12)

        self.search_var = tk.StringVar()
        se = tk.Entry(top, textvariable=self.search_var, bg=BG2, fg=FG,
                      insertbackground=FG, relief="flat", bd=0, width=20,
                      font=self.F(10))
        se.pack(side="right", padx=6, pady=14, ipady=5)
        se.bind("<KeyRelease>", self._on_search)
        self.search_entry = se
        tk.Label(top, text="🔍", bg=BG, fg=FG_MUTE, font=self.F(10)).pack(side="right")

        # 主体
        body = self.body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True)

        # 左侧
        left = self.left_panel = tk.Frame(body, bg=BG, width=344)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        # ★ 分类 / 分组共用「顶部同一槽位」—— 以前分组行排在频道列表后面，
        #   pack 时被挤到左栏最底下，用户看到的就是「分类在顶、分组在底」。
        #   现在两个互斥地占同一格，不管哪个源，选择器永远在同一个位置。
        self.filter_slot = tk.Frame(left, bg=BG)
        self.filter_slot.pack(fill="x")

        # 本机源：分类选择器（★ v2.10.21 从下拉改回三列平铺胶囊 —— 用户要
        #   「平铺胶囊」那种一眼看全、点一下就走的手感，不要藏在下拉里）
        self.catf = tk.Frame(self.filter_slot, bg=BG)
        self.catf.pack(fill="x", padx=12, pady=(10, 8))
        tk.Label(self.catf, text="分类", bg=BG, fg=FG_MUTE,
                 font=self.F(9)).pack(anchor="w", pady=(0, 6))
        self.cat_bar = PillBar(self.catf, cols=3, cell_h=30, gap=6, radius=9,
                               font=self.F(10), on_pick=self._on_cat_pick)
        self.cat_bar.pack(fill="x")

        # 官网源：分组选择器（★ 同上改回平铺胶囊。51 个分组平铺约 646px 高，
        #   会把频道列表挤没 —— 所以限高 4 行、内部滚动，滚轮单独吃）
        self.group_frame = tk.Frame(self.filter_slot, bg=BG)
        gf = tk.Frame(self.group_frame, bg=BG)
        gf.pack(fill="x", padx=12, pady=(10, 6))
        tk.Label(gf, text="分组", bg=BG, fg=FG_MUTE,
                 font=self.F(9)).pack(side="left", padx=(0, 8))
        tk.Button(gf, text="⟳", bg=BG2, fg=FG_DIM, relief="flat", bd=0, padx=8,
                  font=self.F(10), cursor="hand2", activebackground=BRAND,
                  activeforeground="#fff",
                  command=self._reload_wvt).pack(side="right")

        gwrap = tk.Frame(self.group_frame, bg=BG)
        gwrap.pack(fill="x", padx=12, pady=(0, 8))
        gcv = self.group_canvas = tk.Canvas(gwrap, bg=BG, highlightthickness=0,
                                            height=GROUP_BAR_H)
        gsb = ttk.Scrollbar(gwrap, orient="vertical",
                            style="Dark.Vertical.TScrollbar", command=gcv.yview)
        self.group_cbo = PillBar(gcv, cols=3, cell_h=30, gap=6, radius=9,
                                 font=self.F(10), on_pick=self._on_group_pick)
        gwin = gcv.create_window((0, 0), window=self.group_cbo, anchor="nw")
        gcv.configure(yscrollcommand=gsb.set)
        # ★ add="+"：PillBar 自己已经绑了 <Configure>→redraw，这里不能用
        #   默认替换语义，否则它的自适应重绘会被顶掉。
        self.group_cbo.bind(
            "<Configure>",
            lambda e: gcv.configure(scrollregion=gcv.bbox("all")), add="+")
        gcv.bind("<Configure>", lambda e: gcv.itemconfig(gwin, width=e.width))
        # 分组区自己的滚轮：必须 return "break"，否则事件会冒泡到全局
        # bind_all 的频道列表滚轮上（1146 行），变成「滚分组却滚了频道」。
        gcv.bind("<MouseWheel>", self._on_group_wheel)
        gcv.pack(side="left", fill="both", expand=True)
        gsb.pack(side="right", fill="y")
        self.group_frame.pack_forget()      # 默认本机源：分组行先藏起来

        listwrap = tk.Frame(left, bg=BG)
        listwrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(listwrap, bg=BG, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(listwrap, orient="vertical",
                                       style="Dark.Vertical.TScrollbar",
                                       command=self.canvas.yview)
        self.chan_frame = tk.Frame(self.canvas, bg=BG)
        self._win = self.canvas.create_window((0, 0), window=self.chan_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.chan_frame.bind("<Configure>",
                             lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfig(self._win, width=e.width))
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

        # 右侧
        right = self.right_panel = tk.Frame(body, bg="#000")
        right.pack(side="left", fill="both", expand=True)
        self.video_host = tk.Frame(right, bg="#000")
        self.video_host.pack(fill="both", expand=True)
        self.video_lbl = tk.Label(self.video_host, text="点左侧频道开始播放",
                                  bg="#000", fg=FG_MUTE, font=self.F(14))
        self.video_lbl.pack(expand=True)
        # 只绑双击：以前同时绑了单击，点一下就全屏、双击连切三次，状态全乱
        self.video_host.bind("<Double-Button-1>", lambda e: self._toggle_fullscreen())
        # mpv / 内嵌浏览器都是 Win32 子窗口，父窗口变大它们不会自动跟随 ——
        # 全屏切换后必须手动重新铺一次，否则就是「缺画面」。
        self.video_host.bind("<Configure>", self._on_host_resize)

        # ---- 等待动画覆盖层（点击频道后给「点了/在连」的即时反馈）----
        # place 成铺满 video_host 的一层；mpv/WebView2 的原生子窗口永远画在
        # Tk 之上，所以起播成功后画面会自然盖住它 —— 这里再主动收掉，双保险。
        self._load_frame = lf = tk.Frame(self.video_host, bg="#0c0d12")
        inner = tk.Frame(lf, bg="#0c0d12")
        inner.place(relx=0.5, rely=0.5, anchor="center")
        self._load_cv = tk.Canvas(inner, width=46, height=46, bg="#0c0d12",
                                  highlightthickness=0)
        self._load_cv.pack(pady=(0, 14))
        self._load_arc = self._load_cv.create_arc(6, 6, 40, 40, start=0, extent=280,
                                                  style="arc", outline=BRAND2, width=4)
        self._load_name = tk.Label(inner, text="", bg="#0c0d12", fg=FG,
                                   font=self.F(15, True))
        self._load_name.pack()
        self._load_dot = tk.Label(inner, text="", bg="#0c0d12", fg=FG_MUTE,
                                  font=self.F(10))
        self._load_dot.pack(pady=(8, 18))
        self._load_bar = tk.Canvas(inner, width=260, height=4, bg="#0c0d12",
                                   highlightthickness=0)
        self._load_bar.pack()

        # EPG 节目单条
        self.epg_bar = tk.Frame(right, bg=BG2, height=118)
        epg_bar = self.epg_bar
        epg_bar.pack_propagate(False)
        epg_head = tk.Frame(epg_bar, bg=BG2)
        epg_head.pack(fill="x", padx=12, pady=(8, 2))
        self.epg_title = tk.Label(epg_head, text="节目单", bg=BG2, fg=FG,
                                  font=self.F(10, True))
        self.epg_title.pack(side="left")
        self.epg_now = tk.Label(epg_head, text="", bg=BG2, fg=BRAND2, font=self.F(10))
        self.epg_now.pack(side="left", padx=10)
        tk.Button(epg_head, text="✕", bg=BG2, fg=FG_MUTE, relief="flat", bd=0,
                  font=self.F(10), cursor="hand2", command=self._toggle_epg,
                  activebackground=BG3, activeforeground=FG).pack(side="right")
        self.epg_canvas = tk.Canvas(epg_bar, bg=BG2, highlightthickness=0, height=76)
        self.epg_canvas.pack(fill="x", padx=12, pady=(0, 8))
        self.epg_inner = tk.Frame(self.epg_canvas, bg=BG2)
        self._epg_win = self.epg_canvas.create_window((0, 0), window=self.epg_inner,
                                                      anchor="nw")
        self.epg_inner.bind("<Configure>", lambda e: self.epg_canvas.configure(
            scrollregion=self.epg_canvas.bbox("all")))
        self.epg_canvas.bind("<MouseWheel>", self._on_epg_wheel)
        epg_bar.pack_forget()   # 默认收起

        # 底部控制条
        ctrl = self.ctrl_bar = tk.Frame(self.root, bg=BG2, height=74)
        ctrl.pack(fill="x", side="bottom")
        ctrl.pack_propagate(False)
        info = tk.Frame(ctrl, bg=BG2)
        info.pack(side="left", fill="y", padx=16)
        self.chan_name_lbl = tk.Label(info, text="—", bg=BG2, fg=FG,
                                      font=self.F(13, True), anchor="w")
        self.chan_name_lbl.pack(anchor="w", pady=(16, 0))
        sub = tk.Frame(info, bg=BG2)
        sub.pack(anchor="w")
        self.line_lbl = tk.Label(sub, text="", bg=BG2, fg=FG_DIM, font=self.F(9))
        self.line_lbl.pack(side="left")
        self.hw_lbl = tk.Label(sub, text=("· GPU 硬解" if self.mpv else "· 未检测到 mpv"),
                               bg=BG2, fg=FG_MUTE, font=self.F(9))
        self.hw_lbl.pack(side="left", padx=6)

        # 右侧按钮（从右往左 pack）
        def mkbtn(text, cmd, w=None, fg=FG):
            b = tk.Button(ctrl, text=text, bg=BG2, fg=fg, relief="flat", bd=0,
                          padx=12, pady=8, activebackground=BRAND,
                          activeforeground="#fff", font=self.F(10), cursor="hand2",
                          command=cmd)
            b.pack(side="right", padx=4, pady=20)
            return b

        self.fav_btn = mkbtn("☆ 收藏", self._toggle_fav)
        mkbtn("下一线路 ⏭", self._next_line)
        self.pause_btn = mkbtn("⏸ 暂停", self._toggle_pause)
        mkbtn("⏮ 上一线路", self._prev_line)

        # 画面比例
        self.ratio_btn = tk.Menubutton(ctrl, text="画面 适应 ▾", bg=BG2, fg=FG,
                                       relief="flat", bd=0, padx=10, pady=8,
                                       activebackground=BG3, activeforeground=FG,
                                       font=self.F(10), cursor="hand2")
        menu = tk.Menu(self.ratio_btn, tearoff=0, bg=BG3, fg=FG,
                       activebackground=BRAND, activeforeground="#fff", bd=0)
        for r in ("适应", "铺满", "16:9", "4:3"):
            menu.add_command(label=r, command=lambda r=r: self._set_ratio(r))
        self.ratio_btn.configure(menu=menu)
        self.ratio_btn.pack(side="right", padx=4, pady=20)

        # 音量
        self.mute_btn = mkbtn("🔊", self._toggle_mute)
        self.vol_scale = tk.Scale(ctrl, from_=0, to=100, orient="horizontal",
                                  bg=BG2, fg=FG_DIM, troughcolor=BG3,
                                  highlightthickness=0, bd=0, showvalue=False,
                                  activebackground=BRAND, length=110,
                                  command=self._on_volume)
        self.vol_scale.set(100)
        self.vol_scale.pack(side="right", padx=2, pady=20)

        self.epg_btn = mkbtn("节目单", self._toggle_epg)
        mkbtn("全屏", self._toggle_fullscreen)

        self._render_cat_bar()

    # ---------------- 事件绑定 ----------------
    def _bind_keys(self):
        self.root.bind("<space>", lambda e: self._safe_key(self._toggle_pause))
        self.root.bind("<Left>", lambda e: self._safe_key(self._prev_line))
        self.root.bind("<Right>", lambda e: self._safe_key(self._next_line))
        self.root.bind("<Up>", lambda e: self._safe_key(lambda: self._step_channel(-1)))
        self.root.bind("<Down>", lambda e: self._safe_key(lambda: self._step_channel(1)))
        self.root.bind("<F11>", lambda e: self._safe_key(self._toggle_fullscreen))
        self.root.bind("<Escape>", lambda e: self._safe_key(self._exit_fullscreen))
        self.root.bind("<Control-f>", lambda e: (self.search_entry.focus_set(), "break")[1])
        self.root.bind("<Return>", lambda e: self._safe_key(self._toggle_epg))

    def _safe_key(self, fn):
        # 输入框获得焦点时不拦截按键（否则空格无法输入）
        try:
            w = self.root.focus_get()
            if isinstance(w, (tk.Entry, ttk.Entry, ttk.Combobox)):
                return
        except Exception:
            pass
        fn()

    def _on_wheel(self, e):
        self.canvas.yview_scroll(int(-e.delta / 120), "units")

    def _on_group_wheel(self, e):
        """分组胶囊区滚轮：滚自己，并 return "break" 阻止冒泡到频道列表。

        频道列表的滚轮是 bind_all 全局绑的（1146 行），不 break 的话滚分组
        会把频道列表一起滚走。
        """
        self.group_canvas.yview_scroll(int(-e.delta / 120), "units")
        return "break"

    def _on_epg_wheel(self, e):
        self.epg_canvas.xview_scroll(int(-e.delta / 120) * 2, "units")

    def _ui(self, fn):
        # 线程安全：后台线程严禁直接 root.after（跨线程进 Tcl 会在主线程
        # 的 embed 消息泵 DispatchMessage 时撞 Tcl 线程状态 → PyEval_RestoreThread
        # 段错误）。统一入队，主线程 _drain_ui 每 30ms 排水。
        self._ui_q.put(fn)

    def _drain_ui(self):
        try:
            while True:
                fn = self._ui_q.get_nowait()
                try:
                    fn()
                except tk.TclError:
                    pass  # 窗口销毁过程中的回调，忽略
                except Exception:
                    import traceback
                    traceback.print_exc()
        except queue.Empty:
            pass
        if self._closing:
            return
        try:
            self.root.after(30, self._drain_ui)
        except tk.TclError:
            pass  # 窗口已销毁

    # ---------------- 数据源切换 ----------------
    def _set_src(self, mode):
        if mode == self.src_mode:
            return
        self.src_mode = mode
        self.src_bar.set_active(mode)
        self.search_var.set("")
        self.search_kw = ""
        if mode == "wvt":
            # 从本机源切走：停掉 mpv / 藏起内嵌浏览器
            self._stop_mpv()
            if self.embed is not None:
                self.embed.hide()
            self._render_cat_bar_off()
            if not self.wvt_loaded:
                self._load_wvt()
            else:
                # 已加载过：直接按「上次分组」重建列表，不要重新解析一遍
                g = self.cur_group if self.cur_group in self.wvt_groups else (
                    "央视" if "央视" in self.wvt_groups
                    else (self.wvt_groups[0] if self.wvt_groups else ""))
                if g:
                    self.group_cbo.set_active(g)
                    self._load_group(g)
                else:
                    self._load_wvt()
        else:
            # ★ 切回本机源：必须把内嵌浏览器藏起来 —— 它是 Win32 子窗口，
            #   不藏的话会一直压在播放区最上层，mpv 的画面根本看不见。
            self._stop_embed(hide=True)
            self.group_frame.pack_forget()
            self.catf.pack(fill="x", padx=12, pady=(10, 8))
            self._load_cat("tv")

    def _render_cat_bar_off(self):
        """官网源下把「分类」收起来，换成「分组」下拉 —— 都占顶部同一格。"""
        self.catf.pack_forget()
        self.group_frame.pack(fill="x")

    def _reload_wvt(self):
        """点 ⟳：后台强制重新联网拉取。

        这里**刻意不清 `wvt_loaded`** —— 清了会让下面的 seed 走「非静默」路径
        把列表整个重建一遍（数据通常一模一样），白白闪一下。
        """
        if self.src_mode != "wvt":
            return
        self._load_wvt(force=True)

    def _load_wvt(self, force=False):
        """官网源加载：**先出数据，再后台联网刷新**。

        顺序与 JS 侧 shared/web/wvt.js 的 load() 完全对齐
        （本地缓存 -> 内置快照立刻可用 -> 后台联网刷新）。

        ★ v2.9.2 修：此前是「联网 -> 内置快照」，冷启动（别人电脑第一次双击
        exe、无缓存、网络又不佳）要等 config/channels/adapters 三段全跑完才上屏
        —— 实测本机 26s，网络差时每段各吃满 45s 预算（可超 2 分钟）。用户反馈
        「双击打开后官网源没有节目列表」就是这来的。内置快照 0.01s 就有 904 频道
        / 32 条适配规则，不该排在最后。
        """
        # ---- ① 立刻上屏：本地缓存 -> 内置快照（不联网，毫秒级）----
        seed, seed_ads, origin = [], [], "none"
        if _wvt is not None:
            try:
                seed, seed_ads, origin = _wvt.peek(DATA_DIR)
            except Exception as e:          # noqa: BLE001
                print("官网源本地数据读取失败:", e, file=sys.stderr)
        if seed:
            # 已加载过（切源回来 / 点刷新）就走静默，避免列表无谓重建
            self._after_wvt_load(seed, seed_ads,
                                 silent=bool(self.wvt_loaded), pending=True)
        else:
            self.status_lbl.configure(text="加载官网源…")

        # ---- ② 后台联网刷新 ----
        def work():
            if _wvt is None:
                self._ui(lambda: self._after_wvt_load(seed, seed_ads, silent=True))
                return
            chans = []
            try:
                chans = _wvt.load_channels(cache_dir=DATA_DIR, force=force)
            except Exception as e:          # noqa: BLE001
                print("官网源加载失败:", e, file=sys.stderr)
            if chans:
                # 频道表先落地 —— 适配规则还要再等几秒，别拖着一块儿不刷
                self._ui(lambda c=chans: self._after_wvt_load(c, None, silent=True))
            try:
                ads = _wvt.load_adapters(cache_dir=DATA_DIR, force=force)
            except Exception as e:          # noqa: BLE001
                print("适配规则加载失败:", e, file=sys.stderr)
                ads = []
            self._ui(lambda c=chans, a=ads: self._after_wvt_load(
                c or seed, a or seed_ads, silent=True))

        threading.Thread(target=work, daemon=True).start()

    def _set_wvt_status(self, pending=False):
        """官网源状态栏文案（pending=正在后台联网更新）。"""
        if pending:
            text = "官网源 · %s · %d 个频道 · 正在联网更新…" % (
                self.cur_group or "-", len(self.filtered))
        else:
            text = "官网源 · %s · %d 个频道（共 %d）" % (
                self.cur_group or "-", len(self.filtered), len(self.wvt_channels))
        self.status_lbl.configure(text=text)

    def _after_wvt_load(self, chans, ads=None, silent=False, pending=False):
        """归一化频道表。

        内部结构统一为：
            {id, name, tid, group, urls:[str], extra:[str]}
        入参兼容两种形态：
            ① wvt.py 原始形态  urls = [{"url":..., "extra":...}, ...]
            ② 已归一化形态     urls = ["...", ...]      ← 重复调用时必须也能吃

        silent：后台联网回来的刷新（当前分组内容没变就不动列表，避免跳位）
        pending：数据是「先上屏」的缓存/内置快照，后台还在联网更新
        """
        self.wvt_channels = []
        seen = {}
        # 央视频付费频道标记（限免/VIP）—— 快照里 pid -> tag，烤死的静态表，
        # 运行时只是字典查询，零开销。来源见 tools/_scrape_ysp_tags.py。
        try:
            ysp_tags = _wvt.builtin_snapshot().get("tags") or {}
        except Exception:
            ysp_tags = {}
        for c in chans or []:
            name = (c.get("name") or "").strip()
            if not name:
                continue
            urls, extra = [], []
            for u in (c.get("urls") or []):
                if isinstance(u, dict):
                    uu = u.get("url")
                    if uu:
                        urls.append(uu)
                        extra.append(u.get("extra") or "")
                elif isinstance(u, str) and u:
                    urls.append(u)
                    extra.append("")
            if not urls:
                continue
            cid = name
            if cid in seen:                 # 同名频道（不同分组）加后缀区分
                seen[cid] += 1
                cid = "%s#%d" % (name, seen[cid])
            else:
                seen[cid] = 1
            tag = c.get("tag")
            if tag not in ("vip", "mianfei"):
                tag = ""
                for uu in urls:
                    m = re.search(r"yangshipin\.cn/tv/home\?pid=(\d+)", uu)
                    if m and m.group(1) in ysp_tags:
                        tag = ysp_tags[m.group(1)]
                        break
            self.wvt_channels.append({
                "id": cid, "name": name, "tid": "wvt:" + (c.get("group") or ""),
                "group": c.get("group") or "其他",
                "urls": urls, "extra": extra, "tag": tag,
            })
        self.wvt_by_id = {c["id"]: c for c in self.wvt_channels}
        if ads:
            self.wvt_adapters = ads
        groups = []
        for c in self.wvt_channels:
            if c["group"] not in groups:
                groups.append(c["group"])
        self.wvt_groups = groups
        self.group_cbo.set_items([(g, g) for g in groups])
        if not self.wvt_channels:
            if silent:
                return          # 静默刷新拿到空表：保留当前画面，别把已显示的列表清掉
            self.wvt_loaded = False
            self.status_lbl.configure(text="官网源加载失败（无数据）")
            self._clear(self.chan_frame)
            tk.Label(self.chan_frame, text="官网源不可用\n（检查网络后点 ⟳ 重试）",
                     bg=BG, fg=FG_MUTE, font=self.F(10), justify="center").pack(pady=24)
            return
        self.wvt_loaded = True
        # 默认选中上一次的分组，否则「央视」
        want = self.cur_group if self.cur_group in groups else (
            "央视" if "央视" in groups else groups[0])
        self.group_cbo.set_active(want)
        # 后台联网回来的静默刷新：当前分组的内容没变就别重建列表
        # （用户可能正在浏览/滚动，无谓重建会跳位）。变了才刷 —— 网络回来后
        # 频道表常有增删，这时候不刷反而会让列表长期停在旧数据上。
        if silent and self.src_mode == "wvt" and self.cur_group in self.wvt_groups:
            new_ids = [c["id"] for c in self.wvt_channels
                       if c["group"] == self.cur_group]
            if new_ids != [c["id"] for c in self.channels]:
                self._load_group(self.cur_group)
            self._set_wvt_status(pending)
            return
        self._load_group(want)
        if pending:
            self._set_wvt_status(True)

    def _on_group_pick(self, g):
        self._load_group(g)

    def _load_group(self, g):
        self.cur_group = g
        self.search_var.set("")
        self.search_kw = ""
        self.channels = [c for c in self.wvt_channels if c["group"] == g]
        self._apply_filter()
        if self.src_mode == "wvt":
            self.status_lbl.configure(text="官网源 · %s · %d 个频道（共 %d）" % (
                g, len(self.channels), len(self.wvt_channels)))

    # ---------------- 频道列表 ----------------
    def _render_cat_bar(self):
        entries = [(t, n) for t, n in self.cats] + [("fav", "⭐ 收藏")]
        self.cat_bar.set_items(entries, self.cur_cat)

    def _on_cat_pick(self, tid):
        self._load_cat(tid)

    def _load_cat(self, tid):
        self.cur_cat = tid
        self.cat_bar.set_active(tid)
        self._clear(self.chan_frame)

        if tid == "fav":
            self.channels = [{"id": f["id"], "name": f["name"], "tid": f["tid"]}
                             for f in self.fav]
            self._apply_filter()
            self.status_lbl.configure(text="%d 个收藏" % len(self.channels))
            return

        self.status_lbl.configure(text="加载中…")
        threading.Thread(target=self._fetch_channels, args=(tid,), daemon=True).start()

    def _fetch_channels(self, tid):
        try:
            chans = _core.fetch_channels(tid, cache=self.mem_cache)
        except Exception:
            chans = []
            self._ui(lambda: self.status_lbl.configure(text="加载失败（接口不可达）"))
        self.channels = [dict(c, tid=tid) for c in chans]
        self._ui(self._apply_filter)

    def _on_search(self, e=None):
        self.search_kw = self.search_var.get().strip()
        self._apply_filter()

    def _apply_filter(self):
        kw = self.search_kw
        self.filtered = [c for c in self.channels if (kw in c["name"])] if kw else list(self.channels)
        self._render_channels(self.filtered)
        if self.cur_cat == "fav":
            return
        if self.src_mode == "wvt":
            self.status_lbl.configure(text="官网源 · %s · %d 个频道%s" % (
                self.cur_group or "-", len(self.filtered),
                ("（筛选自 %d）" % len(self.channels)) if kw else ""))
            return
        self.status_lbl.configure(text="%d 个频道%s" % (
            len(self.filtered), ("（筛选自 %d）" % len(self.channels)) if kw else ""))

    def _render_channels(self, chans):
        self._clear(self.chan_frame)
        self._rows = {}
        if not chans:
            tk.Label(self.chan_frame, text="（空）", bg=BG, fg=FG_MUTE,
                     font=self.F(10)).pack(pady=20)
            return
        for c in chans:
            self._make_row(c)
        self._mark_selected()

    def _make_row(self, c):
        row = tk.Frame(self.chan_frame, bg=BG, cursor="hand2")
        row.pack(fill="x", padx=8, pady=2)

        # 台标槽
        slot = tk.Frame(row, bg=BG, width=LOGO_SIZE + 4, height=LOGO_SIZE + 4)
        slot.pack(side="left", padx=(4, 8), pady=2)
        slot.pack_propagate(False)
        badge = tk.Label(slot, text=(c["name"][:1] if c["name"] else "?"),
                         bg=badge_color(c["name"]), fg="#fff", font=self.F(12, True))
        badge.pack(fill="both", expand=True)

        url = _core.match_logo(c["name"], self.logos)

        def on_logo(img, badge=badge):
            # 台标是异步下载的：回调触发时该行可能已被 _clear() 销毁（切分类/换分组），
            # 直接 configure 会抛 TclError 刷屏 —— 必须先确认控件还活着。
            if img is None:
                return
            try:
                if not badge.winfo_exists():
                    return
                badge.configure(image=img, text="", bg=BG)
                badge.image = img
            except Exception:
                pass

        self.logo_store.request(url, on_logo)

        name = tk.Label(row, text=c["name"], bg=BG, fg=FG_DIM, font=self.F(10),
                        anchor="w")
        name.pack(side="left", fill="x", expand=True)

        is_fav = any(f["tid"] == c["tid"] and f["id"] == c["id"] for f in self.fav)
        star = tk.Label(row, text="★" if is_fav else "☆", bg=BG,
                        fg=GOLD if is_fav else FG_MUTE, font=self.F(11), cursor="hand2")

        def togg(e, c=c):
            self._toggle_fav_for(c)
            return "break"

        star.bind("<Button-1>", togg)
        star.pack(side="right", padx=(4, 8))

        # 限免/VIP 角标（官网付费频道；在 ★ 左侧，选中/悬停时背景跟随行）
        tag_lbl = None
        if c.get("tag") in ("vip", "mianfei"):
            vip = c["tag"] == "vip"
            tag_lbl = tk.Label(row, text="VIP" if vip else "限免", bg=BG,
                               fg=(GOLD if vip else "#3ddc84"),
                               font=self.F(8, True), cursor="hand2")
            tag_lbl.pack(side="right", padx=(0, 7))

        widgets = (row, slot, badge, name)
        if tag_lbl is not None:
            widgets = widgets + (tag_lbl,)
            row._tag_lbl = tag_lbl
        key = (str(c["tid"]), str(c["id"]))
        self._rows[key] = (row, name)
        row._sel = (key == self._sel_key)
        if row._sel:
            for w in (row, slot, name) + ((tag_lbl,) if tag_lbl else ()):
                w.configure(bg=SEL_BG)
            name.configure(fg=FG)
        for w in widgets:
            w.bind("<Button-1>", lambda e, c=c: self._open_channel(c["tid"], c["id"], c["name"]))
            w.bind("<Enter>", lambda e, r=row, n=name, b=badge, s=slot: self._row_hover(r, n, b, s, True))
            w.bind("<Leave>", lambda e, r=row, n=name, b=badge, s=slot: self._row_hover(r, n, b, s, False))

    def _row_hover(self, row, name, badge, slot, on):
        sel = getattr(row, "_sel", False)
        base = SEL_BG if sel else BG
        bg = BG3 if on else base
        for w in (row, name, badge, slot):
            try:
                w.configure(bg=bg)
            except Exception:
                pass
        tl = getattr(row, "_tag_lbl", None)     # 限免/VIP 角标背景跟随行
        if tl is not None:
            try:
                tl.configure(bg=bg)
            except Exception:
                pass
        name.configure(fg=FG if (on or sel) else FG_DIM)

    @staticmethod
    def _clear(frame):
        for w in frame.winfo_children():
            w.destroy()

    # ---------------- 播放 ----------------
    def _open_channel(self, tid, cid, name):
        self._req += 1
        self.cur["tid"] = tid
        self.cur["id"] = cid
        self.cur["name"] = name
        self.cur["urls"] = []
        self.cur["extra"] = []
        self.cur["idx"] = 0
        self.cur["epg"] = []
        self.cur["src"] = "wvt" if str(tid).startswith("wvt:") else "local"
        self._try_round = 0
        self._refreshed = False
        self.chan_name_lbl.configure(text=name)
        self._update_fav_btn()
        self.epg_title.configure(text="节目单 · " + name)
        self._mark_selected()
        self._show_loading(name, "正在获取线路…")

        if self.cur["src"] == "wvt":
            ch = (self.wvt_by_id or {}).get(cid)
            if not ch:
                self.status_lbl.configure(text="频道信息缺失")
                self._hide_loading()
                return
            self.cur["urls"] = list(ch["urls"])
            self.cur["extra"] = list(ch.get("extra") or [])
            self._render_epg([])
            self.line_lbl.configure(text="线路 1/%d" % max(1, len(self.cur["urls"])))
            self._play_idx(0)
            return

        self.status_lbl.configure(text="正在获取「%s」的线路…" % name)
        threading.Thread(target=self._fetch_play, args=(tid, cid, False), daemon=True).start()

    def _fetch_play(self, tid, cid, refresh):
        try:
            urls, epg, msg = _core.fetch_play(tid, cid)
        except Exception as e:
            urls, epg, msg = [], [], str(e)
        self.cur["urls"] = urls
        self.cur["epg"] = epg
        self._ui(lambda: self._after_fetch(urls, epg, msg, refresh))

    def _after_fetch(self, urls, epg, msg, refresh):
        self._render_epg(epg)
        if not urls:
            self.status_lbl.configure(text="线路解析失败：%s" % msg)
            self._hide_loading()
            return
        self.status_lbl.configure(text="")
        self._play_idx(0)

    def _play_idx(self, idx):
        if not self.cur["urls"]:
            return
        idx %= len(self.cur["urls"])
        self.cur["idx"] = idx
        self.line_lbl.configure(text="线路 %d/%d" % (idx + 1, len(self.cur["urls"])))
        url = self.cur["urls"][idx]
        n = len(self.cur["urls"])
        if self.cur.get("src") == "wvt":
            self._set_loading_sub("线路 %d/%d…" % (idx + 1, n))
            self._play_wvt(url, idx)
            return
        self._set_loading_sub("正在连接线路 %d/%d" % (idx + 1, n))
        self.status_lbl.configure(text="连接中…")
        self._start_mpv(url)

    # ---------------- 官网源播放（直连走 mpv / 网页走内嵌浏览器） ----------------
    def _play_wvt(self, url, idx):
        """
        官网源线路分两类：

          直连流（.m3u8/.flv/...） → 交给 mpv 硬解，最省资源；
          官方直播页               → 把 Edge/Chrome 窗口内嵌进播放区自己播。

        以前网页线路是「另起一个浏览器嗅探出真实流地址再交给 mpv」，
        结果是后台常驻一个浏览器进程（用户看到「莫名多出个浏览器」），
        而且 CCTV 系用 MSE 播 blob: 源，嗅探到的地址 mpv 也播不了。
        """
        extra = ""
        try:
            ex = self.cur.get("extra") or []
            if idx < len(ex):
                extra = ex[idx] or ""
        except Exception:
            pass

        if _wvt is not None and _wvt.is_direct_stream(url):
            self._stop_embed(hide=True)
            self.status_lbl.configure(text="直连线路，正在播放…")
            self._start_mpv(url)
            return

        self._show_web(url, extra)

    def _load_inject(self):
        if self._inject_src is not None:
            return self._inject_src
        src = ""
        for base in (_MEIPASS, _HERE, os.path.join(_HERE, "..")):
            p = os.path.join(base, "shared", "web", "wvt_inject.js")
            if os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        src = f.read()
                except Exception:
                    src = ""
                break
        self._inject_src = src
        return src

    def _show_web(self, url, extra):
        """网页线路：把浏览器嵌进播放区，在官方页面上注入脚本直接播。"""
        inject = self._load_inject()
        if _embed_mod is None or not inject or _embed_mod.find_browser() is None:
            self.status_lbl.configure(text="WebView2 运行时不可用，改用解析模式…")
            self._sniff_fallback(url, extra)
            return

        self._stop_mpv()
        self.video_lbl.pack_forget()
        self._web_state = ""
        self._web_t0 = time.time()      # 原生看门狗起点（每条线路重置）
        self.status_lbl.configure(text="正在打开官方直播页…")
        self._set_loading_sub("正在打开官方直播页")

        ad = None
        try:
            if _wvt is not None and self.wvt_adapters:
                ad = _wvt.match_adapter(url, self.wvt_adapters)
        except Exception:
            ad = None
        if not ad:
            ad = {"url": "", "ua": "PC",
                  "videoElementCondition": "e.tagName === 'VIDEO'"}
        json_ad = json.dumps(ad, ensure_ascii=False)

        self._embed_seq += 1
        seq = self._embed_seq
        if self.embed is None:
            # ★ schedule：把作业排到 Tk 主线程执行 —— WebView2 的创建回调只认
            #   父窗口线程的消息循环（主线程），这是官方线程模型的要求。
            self.embed = _embed_mod.EmbeddedBrowser(
                DATA_DIR, log=self._log,
                schedule=lambda ms, fn: self.root.after(int(ms), fn))
        emb = self.embed
        host = self.video_host
        host.update_idletasks()
        hw = host.winfo_id()
        w = max(64, host.winfo_width())
        h = max(64, host.winfo_height())

        def work():
            ok = False
            try:
                ok = emb.play(url, json_ad, extra, inject, hw, w, h)
            except Exception as e:      # noqa: BLE001
                self._log("内嵌浏览器异常: %r" % (e,))
            self._ui(lambda: self._after_web_open(ok, seq))

        threading.Thread(target=work, daemon=True).start()

    def _sniff_fallback(self, url, extra, last_resort=False):
        """没有 Edge/Chrome 的机器上的退路：嗅探真实流地址交给 mpv。"""
        if _cdp is None:
            self.status_lbl.configure(text="当前环境无法播放官网网页线路")
            return
        req = self._req
        self.status_lbl.configure(text="正在解析官网流…（首次需 10~30 秒）")

        def work():
            r = None
            try:
                r = _cdp.sniff_stream(
                    url, timeout=26, offscreen=True,
                    profile_dir=self.sniff_dir,
                    log=lambda *a: self._log(" ".join(str(x) for x in a)))
            except Exception as e:      # noqa: BLE001
                self._log("嗅探异常: %r" % (e,))
            self._ui(lambda: self._after_sniff(r, url, req, extra,
                                               last_resort=last_resort))

        threading.Thread(target=work, daemon=True).start()

    def _after_web_open(self, ok, seq):
        if seq != self._embed_seq:
            return
        if not ok:
            self.status_lbl.configure(text="官方页面打不开，试下一条线路…")
            self._next_line()
            return
        self.status_lbl.configure(text="官方页面已加载，正在起播…")
        self._web_watch(seq)

    def _web_watch(self, seq):
        """后台每秒问一次页面：playing / failed / closed（failed 附带原因）。"""
        emb = self.embed

        def loop():
            while seq == self._embed_seq:
                time.sleep(1.0)
                if emb is None or not emb.alive():
                    return
                try:
                    st = emb.state()
                except Exception:
                    st = ""
                rs = ""
                if st == "failed":
                    try:
                        rs = emb.page_reason()
                    except Exception:
                        rs = ""
                # ★ 原生看门狗：页面迟迟不自报状态就由原生判死。
                #   页面 JS 把渲染器主线程拖死时，ExecuteScript 回调整体停摆，
                #   注入脚本的 20s 超时永远送不出来 —— 用户只会看到黑屏干等
                #   （v2.4.6 实测复现）。35s 无任何汇报 = 这条线路没戏，换线。
                if (st == "" and self._web_state == ""
                        and time.time() - getattr(self, "_web_t0", time.time())
                        > 35):
                    st, rs = "failed", "页面长时间无响应"
                self._ui(lambda s=st, r=rs: self._on_web_state(s, seq, r))

        threading.Thread(target=loop, daemon=True).start()

    def _on_web_state(self, st, seq, reason=""):
        if seq != self._embed_seq or st == self._web_state:
            return
        self._web_state = st
        self._log("页面状态变化：%s%s（seq=%d 线路%d/%d）"
                  % (st or "(空)", "（" + reason + "）" if reason else "",
                     seq, self.cur["idx"] + 1,
                     max(1, len(self.cur["urls"]))))
        if st == "playing":
            # 页面真的出画面了 —— 再确认一次窗口是可见的（换台复用同一个浏览器窗口时，
            # 导航过程可能让 Chromium 把窗口置回隐藏，位置尺寸都对但看不见）。
            if self.embed is not None:
                self.embed.show()
            self.status_lbl.configure(text="官网源 · 内嵌浏览器播放中")
            self._hide_loading()
        elif st == "failed":
            why = ("（%s）" % reason) if reason else ""
            if self.cur["idx"] + 1 < len(self.cur["urls"]):
                self.status_lbl.configure(
                    text="该线路不可用%s，自动切下一条…" % why)
                self._set_loading_sub(reason or "该线路起不来")
                self._play_idx(self.cur["idx"] + 1)
            else:
                # ★ 官网网页线路全部失败。VIP 类限制直接收工 —— 嗅探兜底对付费
                #   流毫无办法（DRM/鉴权），试了也是白等 10~30 秒。
                vip_only = ("会员" in reason) or ("VIP" in reason.upper())
                self._log("网页线路全部失败（原因：%s），vip_only=%s：%s"
                          % (reason or "未知", vip_only, self.cur["name"]))
                self._stop_embed()
                url0 = ""
                try:
                    url0 = self.cur["urls"][0].get("url", "") \
                        if isinstance(self.cur["urls"][0], dict) \
                        else str(self.cur["urls"][0])
                except Exception:
                    pass
                if url0 and _cdp is not None and not vip_only:
                    self.status_lbl.configure(text="官网线路均不可用，改用流解析直连…")
                    self._set_loading_sub("正在解析官网流")
                    self._sniff_fallback(url0, "", last_resort=True)
                else:
                    self._show_web_dead(reason)
        elif st == "closed":
            self._stop_embed()
            self._show_placeholder("已退出官方页面")

    def _show_web_dead(self, reason=""):
        """官网线路全部没戏了 —— 给出明确文字，不黑屏不干等。"""
        why = ("（%s）" % reason) if reason else ""
        self.status_lbl.configure(text="该频道官网线路均不可用%s" % why)
        self._show_placeholder("该频道官网线路均不可用\n%s\n换个频道或按 → 试相邻频道"
                               % ("「%s」" % reason if reason else ""))

    def _stop_embed(self, hide=False, wait=False):
        """
        停掉内嵌浏览器。

        hide=True  → 只藏窗口（切回本机源，进程留着秒切回来），非阻塞；
        wait=True  → 阻塞等到进程真的退出（退出应用时用，避免残留后台浏览器）；
        默认       → 后台线程异步 close（播放页自己报告 closed 时用，别卡 UI）。
        """
        self._embed_seq += 1
        self._web_state = ""
        if self.embed is None:
            return
        if hide:
            self.embed.hide()
        elif wait:
            self.embed.close()
        else:
            emb = self.embed
            threading.Thread(target=emb.close, daemon=True).start()

    def _show_placeholder(self, text="点左侧频道开始播放"):
        try:
            self.video_lbl.configure(text=text)
            self.video_lbl.pack(expand=True)
        except Exception:
            pass
        self._hide_loading()

    # ---------------- 等待反馈（点击频道 → 起播之间的过渡） ----------------
    def _show_loading(self, name, sub="正在连接…"):
        """在播放区铺一层等待动画：频道名 + 旋转弧 + 步进提示 + 流动细条。

        mpv / WebView2 的原生子窗口永远画在 Tk 控件之上，所以真正起播后
        画面会自然盖住这层 —— 这里在子窗口出现/收到 playing 时再主动收掉。
        """
        self._load_name.configure(text=name)
        self._set_loading_sub(sub)
        f = self._load_frame
        f.place(relx=0, rely=0, relwidth=1, relheight=1)
        f.lift()
        self._load_t0 = time.time()
        if self._load_after is None:
            self._load_tick()

    def _set_loading_sub(self, sub):
        try:
            self._load_base = sub
            self._load_dot.configure(text=sub)
        except Exception:
            pass

    def _hide_loading(self):
        f = getattr(self, "_load_frame", None)
        if f is None:
            return
        try:
            if f.winfo_ismapped():
                f.place_forget()
        except Exception:
            pass

    def _load_tick(self):
        """50ms 一拍：转弧 + 流动条 + 提示文字加步进点。仅覆盖层可见时运行。"""
        self._load_after = None
        f = getattr(self, "_load_frame", None)
        if f is None:
            return
        try:
            if not f.winfo_ismapped():
                return
            self._load_spin = (self._load_spin + 14) % 360
            self._load_cv.itemconfigure(self._load_arc, start=self._load_spin)
            cv = self._load_bar
            w = cv.winfo_width()
            if w < 10:
                w = 260
            if self._load_bar_w != w or self._load_bar_rect is None:
                cv.delete("all")
                self._load_bar_rect = cv.create_rectangle(0, 0, 46, 4,
                                                          fill=BRAND2, width=0)
                self._load_bar_w = w
            x = int((time.time() * 130) % (w + 46)) - 46
            cv.coords(self._load_bar_rect, x, 0, x + 46, 4)
            el = int(time.time() - self._load_t0)
            self._load_dot.configure(text=self._load_base + "." * (1 + el % 3))
            self._load_after = self.root.after(50, self._load_tick)
        except Exception:
            self._load_after = None

    def _mpv_child(self):
        """video_host 下是否已出现 mpv/WebView2 的原生子窗口（>100x100）。

        mpv 的视频子窗口在 vo 初始化（≈ 第一帧上屏）时才创建 —— 拿它当
        「真的出画面了」的信号，比进程存活准确得多。
        """
        if not IS_WIN:
            return True             # Linux/X11 没法枚举，直接当就绪（收掉等待层）
        try:
            import ctypes
            from ctypes import wintypes
            host = self.video_host.winfo_id()
        except Exception:
            return False
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HWND, wintypes.LPARAM)
        def cb(hwnd, _lp):
            r = wintypes.RECT()
            if ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)):
                if r.right - r.left > 100 and r.bottom - r.top > 100:
                    found.append(hwnd)
            return True

        ctypes.windll.user32.EnumChildWindows(host, cb, 0)
        return bool(found)

    def _wait_first_frame(self, gen, timeout=30):
        """后台轮询：mpv 视频子窗口出现（或超时兜底）就收掉等待层。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            if gen != self._mpv_gen:
                return              # 已换台/停止：新一轮有它自己的等待层
            if self._mpv_child():
                break
            time.sleep(0.25)
        self._ui(self._hide_loading)

    # ---------------- 选中高亮 ----------------
    def _mark_selected(self):
        """给「点没点上」一个明确的视觉反馈：选中行用品牌暗红底。"""
        key = (str(self.cur.get("tid")), str(self.cur.get("id")))
        self._sel_key = key
        for k, (row, name) in self._rows.items():
            sel = k == key
            try:
                if getattr(row, "_sel", False) != sel:
                    row._sel = sel
                    bg = SEL_BG if sel else BG
                    row.configure(bg=bg)
                    name.configure(bg=bg, fg=FG if sel else FG_DIM)
                    tl = getattr(row, "_tag_lbl", None)   # 角标背景跟随
                    if tl is not None:
                        tl.configure(bg=bg)
            except Exception:
                pass

    def _after_sniff(self, r, page_url, req, extra="", last_resort=False):
        if req != self._req:            # 用户已换台，丢弃过期结果
            return
        if r and r.get("url"):
            self._log("嗅探成功: %s" % r["url"])
            self.status_lbl.configure(text="已解析到官网流，正在播放…")
            self._set_loading_sub("已解析到流，交给播放器")
            self._start_mpv(r["url"], headers=r.get("headers"))
            return
        if last_resort:
            self._log("嗅探兜底也失败，放弃：%s" % self.cur["name"])
            self._show_web_dead("")
            return
        self.status_lbl.configure(text="未能解析流地址，试试下一条线路")


    def _log(self, s):
        try:
            with open(os.path.join(DATA_DIR, "cleantv.log"), "a", encoding="utf-8") as f:
                f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), s))
        except Exception:
            pass

    def _next_line(self):
        if self.cur["urls"]:
            self._play_idx(self.cur["idx"] + 1)

    def _prev_line(self):
        if self.cur["urls"]:
            self._play_idx(self.cur["idx"] - 1)

    def _step_channel(self, d):
        if not self.filtered:
            return
        names = [c["id"] for c in self.filtered]
        try:
            i = names.index(self.cur["id"])
        except ValueError:
            i = -1
        j = (i + d) % len(self.filtered)
        c = self.filtered[j]
        self._open_channel(c["tid"], c["id"], c["name"])

    # ---------------- mpv 生命周期 ----------------
    def _pipe_path(self):
        if IS_WIN:
            return r"\\.\pipe\cleantv_mpv_%d" % (self._mpv_gen + 1)
        return os.path.join(DATA_DIR, "mpv_%d.sock" % (self._mpv_gen + 1))

    def _start_mpv(self, url, headers=None):
        self._log("mpv 启动请求: mpv=%s url=%s" % (self.mpv, url[:80]))
        if not self.mpv:
            self.status_lbl.configure(text="未检测到 mpv —— 无法播放")
            return
        self._stop_mpv()
        self._mpv_gen += 1
        gen = self._mpv_gen
        self._paused = False
        self._ui(lambda: self.pause_btn.configure(text="⏸ 暂停"))
        # 等窗口就绪再取 HWND
        self.root.after(60, lambda: self._spawn_mpv(url, gen, headers))

    def _spawn_mpv(self, url, gen, headers=None):
        if gen != self._mpv_gen:
            return
        tier = self.settings.get("buffer", "balanced")
        if tier not in _core.BUFFER_TIERS:
            tier = "balanced"
        cache_secs, maxb, maxbb = _core.BUFFER_TIERS[tier]
        pipe = self._pipe_path()
        try:
            self.video_host.update_idletasks()
            wid = str(self.video_host.winfo_id())
            args = [self.mpv, url,
                    "--wid=" + wid,
                    "--hwdec=auto-safe", "--vo=gpu",
                    "--cache=yes", "--cache-secs=%d" % cache_secs,
                    "--demuxer-max-bytes=" + maxb,
                    "--demuxer-max-back-bytes=" + maxbb,
                    "--network-timeout=12",
                    "--input-ipc-server=" + pipe,
                    "--no-border", "--no-osc", "--cursor-autohide=no",
                    "--profile=fast", "--really-quiet"]
            # 官网源嗅探出来的流通常校验 Referer/Cookie，必须带上
            hf = self._header_fields(headers)
            if hf:
                args.append("--http-header-fields=" + hf)
            extra = (self.settings.get("mpv_extra") or "").strip()
            if extra:
                args += extra.split()
            self.mpv_proc = subprocess.Popen(
                args, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True)
            self._log("mpv 已启动 pid=%s" % self.mpv_proc.pid)
            self.video_lbl.pack_forget()
            self.status_lbl.configure(text="")
            # 等待层先留着，等 mpv 的视频子窗口真正出现（≈第一帧上屏）再收
            threading.Thread(target=self._wait_first_frame, args=(gen,), daemon=True).start()
            # 连 IPC（后台，连上后套用当前音量/画面比例）
            self.ipc = MpvIPC(pipe)
            threading.Thread(target=self._ipc_connect, args=(self.ipc,), daemon=True).start()
            # 看门狗：异常退出 -> 切线
            threading.Thread(target=self._watch_mpv, args=(self.mpv_proc, gen), daemon=True).start()
        except Exception as e:
            self.status_lbl.configure(text="mpv 启动失败：%s" % e)

    @staticmethod
    @staticmethod
    def _header_fields(headers):
        """把嗅探到的请求头转成 mpv 的 --http-header-fields 值。

        ★ mpv 列表选项以逗号分隔条目，值里的逗号（典型如 UA 的
          "KHTML, like Gecko"）必须转义成 \\, 否则整个选项解析失败
          （mpv 退出码 2 秒退，无任何提示——实测踩坑）。
        """
        if not headers:
            return ""
        keep = ("referer", "user-agent", "cookie", "origin")
        parts = []
        for k, v in headers.items():
            if k.lower() in keep and v and "\n" not in str(v):
                parts.append("%s: %s" % (k, str(v).replace(",", "\\,")))
        return ",".join(parts)

    def _ipc_connect(self, ipc):
        if not ipc.connect():
            return
        ipc.command("set_property", "volume", self._volume if not self._muted else 0)
        self._apply_ratio(ipc)

    def _watch_mpv(self, proc, gen):
        rc = proc.wait()
        if gen != self._mpv_gen:      # 主动切换/停止，忽略
            return
        # 异常退出：先试下一条线路，全试完则刷新 token 再来一轮
        self._ui(lambda: self._on_line_fail(rc))

    def _on_line_fail(self, rc):
        if not self.cur["urls"]:
            return
        n = len(self.cur["urls"])
        nxt = self.cur["idx"] + 1
        if nxt < n:
            self.status_lbl.configure(text="线路 %d 失败，切换下一条…" % (self.cur["idx"] + 1))
            self._play_idx(nxt)
            return
        if self.cur.get("src") == "wvt":
            # 官网源：整轮重来一次（每次嗅探拿到的 CDN 地址可能不同）
            if not self._refreshed:
                self._refreshed = True
                self.status_lbl.configure(text="全部线路失败，重新解析…")
                self._play_idx(0)
                return
            self.status_lbl.configure(text="该频道暂不可用（官网线路均失败）")
            self._hide_loading()
            return
        # 本机源：所有线路都失败 -> 刷新页面拿新 token 再重试一轮
        if not self._refreshed:
            self._refreshed = True
            self._try_round += 1
            self.status_lbl.configure(text="全部线路失败，刷新线路表重试…")
            tid, cid = self.cur["tid"], self.cur["id"]
            threading.Thread(target=self._fetch_play, args=(tid, cid, True), daemon=True).start()
            return
        self.status_lbl.configure(text="该频道暂不可用（线路均失败）")
        self._hide_loading()

    def _stop_mpv(self):
        if self.ipc:
            self.ipc.close()
            self.ipc = None
        if self.mpv_proc:
            try:
                self.mpv_proc.terminate()
                self.mpv_proc.wait(timeout=2)
            except Exception:
                try:
                    self.mpv_proc.kill()
                except Exception:
                    pass
            self.mpv_proc = None

    # ---------------- 控制 ----------------
    def _toggle_pause(self):
        if not self.ipc:
            return
        self._paused = not self._paused
        self.ipc.command("set_property", "pause", self._paused)
        self.pause_btn.configure(text="▶ 播放" if self._paused else "⏸ 暂停")

    def _on_volume(self, v):
        try:
            self._volume = int(float(v))
        except Exception:
            return
        if self.ipc:
            self.ipc.command("set_property", "volume", 0 if self._muted else self._volume)

    def _toggle_mute(self):
        self._muted = not self._muted
        if self.ipc:
            self.ipc.command("set_property", "volume", 0 if self._muted else self._volume)
        self.mute_btn.configure(text="🔇" if self._muted else "🔊")

    def _set_ratio(self, r):
        self._ratio = r
        self.ratio_btn.configure(text="画面 %s ▾" % r)
        if self.ipc:
            self._apply_ratio(self.ipc)

    def _apply_ratio(self, ipc):
        r = self._ratio
        if r == "适应":
            ipc.command("set_property", "video-aspect-override", "no")
            ipc.command("set_property", "keepaspect", True)
        elif r == "铺满":
            ipc.command("set_property", "video-aspect-override", "no")
            ipc.command("set_property", "keepaspect", False)
        elif r == "16:9":
            ipc.command("set_property", "video-aspect-override", "16:9")
            ipc.command("set_property", "keepaspect", True)
        elif r == "4:3":
            ipc.command("set_property", "video-aspect-override", "4:3")
            ipc.command("set_property", "keepaspect", True)

    # ---------------- EPG ----------------
    def _toggle_epg(self):
        if self.epg_bar.winfo_ismapped():
            self.epg_bar.pack_forget()
        else:
            self.epg_bar.pack(fill="x", side="bottom")
            self._render_epg(self.cur["epg"])

    def _render_epg(self, epg):
        self._clear(self.epg_inner)
        self.epg_now.configure(text="")
        if not epg:
            tk.Label(self.epg_inner, text="暂无节目单", bg=BG2, fg=FG_MUTE,
                     font=self.F(9)).pack(side="left", padx=6, pady=18)
            return
        now_i = self._now_index(epg)
        for i, it in enumerate(epg):
            cur = (i == now_i)
            chip = tk.Frame(self.epg_inner, bg=BRAND if cur else BG3)
            chip.pack(side="left", padx=3, pady=8)
            tk.Label(chip, text=it["time"], bg=BRAND if cur else BG3,
                     fg="#fff" if cur else FG_DIM, font=self.F(9),
                     padx=8, pady=2).pack(anchor="w")
            tk.Label(chip, text=it["name"][:14], bg=BRAND if cur else BG3,
                     fg="#fff" if cur else FG, font=self.F(9, cur),
                     padx=8, pady=2).pack(anchor="w")
            if cur:
                self.epg_now.configure(text="正在播：%s" % it["name"])

    @staticmethod
    def _now_index(epg):
        now = time.strftime("%H:%M")
        idx = 0
        for i, it in enumerate(epg):
            if it["time"] <= now:
                idx = i
            else:
                break
        return idx

    # ---------------- 收藏 ----------------
    def _toggle_fav(self):
        if not self.cur["id"]:
            return
        self._toggle_fav_for({"tid": self.cur["tid"], "id": self.cur["id"],
                              "name": self.cur["name"]})

    def _toggle_fav_for(self, c):
        tid, cid = c["tid"], c["id"]
        if any(f["tid"] == tid and f["id"] == cid for f in self.fav):
            self.fav = [f for f in self.fav if not (f["tid"] == tid and f["id"] == cid)]
        else:
            self.fav.insert(0, {"tid": tid, "id": cid, "name": c["name"]})
        _core.save_fav(self.fav_file, self.fav)
        self._update_fav_btn()
        if self.cur_cat == "fav":
            self._load_cat("fav")
        else:
            self._render_channels(self.filtered)

    def _update_fav_btn(self):
        on = any(f["tid"] == self.cur["tid"] and f["id"] == self.cur["id"] for f in self.fav)
        self.fav_btn.configure(text="★ 已收藏" if on else "☆ 收藏")

    # ---------------- 全屏 / 尺寸跟随 ----------------
    def _on_host_resize(self, e=None):
        """播放区尺寸变了 → 稍后统一把 mpv / 浏览器窗口重新铺满。"""
        if self._fit_job is not None:
            try:
                self.root.after_cancel(self._fit_job)
            except Exception:
                pass
        self._fit_job = self.root.after(70, self._fit_children)

    def _fit_children(self):
        self._fit_job = None
        try:
            host = self.video_host
            w = max(2, host.winfo_width())
            h = max(2, host.winfo_height())
            if _embed_mod is not None:
                _embed_mod.cover_child_windows(host.winfo_id())
            if self.embed is not None:
                self.embed.size = (w, h)
        except Exception:
            pass

    def _toggle_fullscreen(self):
        self._set_fullscreen(not self._fullscreen)

    def _set_fullscreen(self, on):
        if on == self._fullscreen:
            return
        self._fullscreen = on
        if on:
            self._fs_saved_epg = bool(self.epg_bar.winfo_ismapped())
            # body / right_panel 保持 pack（视频在全屏里还要继续显示），
            # 只收顶栏 / 控制条 / 左侧列表 / 节目单。
            for w in (self.top_bar, self.ctrl_bar, self.left_panel, self.epg_bar):
                w.pack_forget()
            self.root.attributes("-fullscreen", True)
            self._show_fs_overlay()
            self.status_lbl.configure(text="")       # 沉浸模式不显示状态文案
        else:
            self._hide_fs_overlay()
            self.root.attributes("-fullscreen", False)
            # ★ 退出时先把 body / right_panel 从 pack 队列摘下来再按初始顺序重排。
            #   pack() 对已在队列里的控件不会挪位置，漏这一步的话 left_panel 会
            #   被追加到 right_panel 后面（频道列表跑到右边）、top_bar 被追加到
            #   body 后面（顶栏跑到窗口中部）—— v2.4.2 实测两个 bug。
            self.right_panel.pack_forget()
            self.body.pack_forget()
            self.top_bar.pack(fill="x", side="top")
            self.body.pack(fill="both", expand=True)
            self.ctrl_bar.pack(fill="x", side="bottom")
            self.left_panel.pack(side="left", fill="y")
            self.right_panel.pack(side="left", fill="both", expand=True)
            if self._fs_saved_epg:
                self.epg_bar.pack(fill="x", side="bottom")
        # 窗口尺寸变了，mpv / 浏览器都要重新铺一次
        self.root.after(120, self._fit_children)
        try:
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass

    def _exit_fullscreen(self):
        if self._fullscreen:
            self._set_fullscreen(False)

    # 沉浸模式下的浮层（全屏后控制条都收起来了，留一个能点的小按钮）
    def _show_fs_overlay(self):
        if self._fs_overlay is not None:
            try:
                self._fs_overlay.deiconify()
                self._fs_overlay.lift()
                return
            except Exception:
                self._fs_overlay = None
        ov = tk.Toplevel(self.root)
        self._fs_overlay = ov
        ov.overrideredirect(True)
        ov.attributes("-topmost", True)
        ov.configure(bg=BG2)
        tk.Label(ov, text="", bg=BG2).pack(side="left", padx=2)
        self._fs_name = tk.Label(ov, text="", bg=BG2, fg=FG, font=self.F(10, True))
        self._fs_name.pack(side="left", padx=8)
        tk.Button(ov, text="⏸", bg=BG2, fg=FG, relief="flat", bd=0, padx=10,
                  font=self.F(10), cursor="hand2", activebackground=BG3,
                  activeforeground="#fff", command=self._toggle_pause).pack(side="left")
        tk.Button(ov, text="⤢ 退出全屏", bg=BG2, fg=FG, relief="flat", bd=0, padx=12,
                  font=self.F(10), cursor="hand2", activebackground=BRAND,
                  activeforeground="#fff",
                  command=self._exit_fullscreen).pack(side="left", padx=2)
        try:
            sw = self.root.winfo_screenwidth()
            ov.update_idletasks()
            ow = ov.winfo_reqwidth()
            ov.geometry("+%d+%d" % (max(0, sw - ow - 24), 18))
        except Exception:
            pass
        self._update_fs_overlay()

    def _update_fs_overlay(self):
        try:
            if self._fs_overlay is not None:
                self._fs_name.configure(text=self.cur.get("name") or "纯净电视直播")
                self._fs_overlay.attributes("-topmost", True)
        except Exception:
            pass

    def _hide_fs_overlay(self):
        if self._fs_overlay is not None:
            try:
                self._fs_overlay.withdraw()
            except Exception:
                pass

    # 内嵌浏览器拿到焦点时，Tk 的键盘绑定收不到按键 —— 用 GetAsyncKeyState
    # 轮询兜住 F11 / Esc，保证「退出全屏」在任何时候都好使。
    def _poll_hotkeys(self):
        try:
            import ctypes
            u = ctypes.windll.user32
            root_hwnd = self.root.winfo_id()
            fg = u.GetForegroundWindow()
            # 只要是「本进程的窗口」就算在前台：主窗口、全屏浮层（独立 Toplevel）、
            # 内嵌的 Chromium 子窗口，全都是同一个 PID。
            pid = ctypes.c_ulong(0)
            u.GetWindowThreadProcessId(fg, ctypes.byref(pid))
            mine = (fg == root_hwnd) or bool(u.IsChild(root_hwnd, fg)) \
                or (pid.value == os.getpid())
            if mine:
                f11 = bool(u.GetAsyncKeyState(0x7A) & 0x8000)
                esc = bool(u.GetAsyncKeyState(0x1B) & 0x8000)
                pf, pe = self._hotkey_prev
                if f11 and not pf:
                    self._toggle_fullscreen()
                if esc and not pe and self._fullscreen:
                    self._exit_fullscreen()
                self._hotkey_prev = (f11, esc)
        except Exception:
            pass
        try:
            self.root.after(140, self._poll_hotkeys)
        except Exception:
            pass

    # ---------------- 设置 ----------------
    def _open_settings(self):
        SettingsDialog(self)

    def _reload_settings(self):
        self.settings = _core.load_settings(self.settings_file)
        _core.apply_settings(self.settings)

    # ---------------- 关闭 ----------------
    def _on_close(self):
        self._closing = True
        self._stop_mpv()
        # ★ 只销毁窗口，不同步等浏览器退出 —— close() 的等待泵绝不能跑在
        #   Tk 回调里（DispatchMessage 嵌套进 Tcl 会撞 GIL → PyEval_RestoreThread
        #   段错误）。同步 close 挪到 mainloop 退出之后（run() 里），那时已无
        #   Tk 回调上下文，嵌套泵安全。
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        try:
            self.root.mainloop()
        finally:
            # mainloop 已退出：无 Tk 回调，可以安全地同步等浏览器退出
            # （避免「关掉软件还留个浏览器进程」）
            try:
                self._stop_embed(wait=True)
            except Exception:
                pass


# ------------------------------------------------------------------ 设置对话框
class SettingsDialog:
    def __init__(self, app):
        self.app = app
        s = app.settings
        self.top = tk.Toplevel(app.root)
        self.top.title("设置")
        self.top.configure(bg=BG)
        self.top.geometry("560x480")
        self.top.transient(app.root)
        self.top.resizable(False, False)

        pad = {"padx": 18, "pady": 6}
        tk.Label(self.top, text="设置", bg=BG, fg=FG,
                 font=app.F(14, True)).pack(anchor="w", padx=18, pady=(16, 4))

        # 接口域名
        self._label("接口域名（留空=自动回退镜像）")
        self.host_var = tk.StringVar(value=s.get("host", ""))
        host_box = tk.Frame(self.top, bg=BG)
        host_box.pack(fill="x", **pad)
        cb = ttk.Combobox(host_box, textvariable=self.host_var,
                          values=[""] + _core.API_MIRRORS, style="Dark.TCombobox",
                          font=app.F(10))
        cb.pack(side="left", fill="x", expand=True)
        tk.Button(host_box, text="测试", bg=BG3, fg=FG, relief="flat", bd=0,
                  padx=12, font=app.F(9), cursor="hand2",
                  command=self._test_host).pack(side="left", padx=6)
        self.host_tip = tk.Label(self.top, text="", bg=BG, fg=FG_MUTE, font=app.F(9))
        self.host_tip.pack(anchor="w", padx=18)

        # UA
        self._label("User-Agent（留空=内置 App UA）")
        self.ua_var = tk.StringVar(value=s.get("ua", ""))
        e = tk.Entry(self.top, textvariable=self.ua_var, bg=BG2, fg=FG,
                     insertbackground=FG, relief="flat", bd=0, font=app.F(9))
        e.pack(fill="x", ipady=6, **pad)

        # 缓冲档位
        self._label("播放缓冲档位")
        self.buf_var = tk.StringVar(value=s.get("buffer", "balanced"))
        bf = tk.Frame(self.top, bg=BG)
        bf.pack(fill="x", **pad)
        for val, txt in (("low", "低延迟"), ("balanced", "均衡（默认）"), ("smooth", "抗卡顿")):
            ttk.Radiobutton(bf, text=txt, value=val, variable=self.buf_var,
                            style="Dark.TRadiobutton").pack(side="left", padx=(0, 14))

        # 缓存 TTL
        self._label("频道列表缓存（秒）")
        self.ttl_var = tk.StringVar(value=str(s.get("ttl", 600)))
        e2 = tk.Entry(self.top, textvariable=self.ttl_var, bg=BG2, fg=FG,
                      insertbackground=FG, relief="flat", bd=0, font=app.F(10), width=12)
        e2.pack(anchor="w", ipady=6, **pad)

        # mpv 附加参数
        self._label("mpv 附加参数（高级，空格分隔）")
        self.mpv_var = tk.StringVar(value=s.get("mpv_extra", ""))
        e3 = tk.Entry(self.top, textvariable=self.mpv_var, bg=BG2, fg=FG,
                      insertbackground=FG, relief="flat", bd=0, font=app.F(9))
        e3.pack(fill="x", ipady=6, **pad)

        # 按钮
        btns = tk.Frame(self.top, bg=BG)
        btns.pack(fill="x", side="bottom", pady=16, padx=18)
        tk.Button(btns, text="保存", bg=BRAND, fg="#fff", relief="flat", bd=0,
                  padx=22, pady=8, font=app.F(10, True), cursor="hand2",
                  activebackground=BRAND2, activeforeground="#fff",
                  command=self._save).pack(side="right")
        tk.Button(btns, text="取消", bg=BG3, fg=FG, relief="flat", bd=0,
                  padx=18, pady=8, font=app.F(10), cursor="hand2",
                  command=self.top.destroy).pack(side="right", padx=8)
        tk.Button(btns, text="恢复默认", bg=BG3, fg=FG_DIM, relief="flat", bd=0,
                  padx=14, pady=8, font=app.F(10), cursor="hand2",
                  command=self._reset).pack(side="left")

    def _label(self, t):
        tk.Label(self.top, text=t, bg=BG, fg=FG_DIM,
                 font=self.app.F(9)).pack(anchor="w", padx=18, pady=(10, 0))

    def _test_host(self):
        h = (self.host_var.get() or "").strip()
        self.host_tip.configure(text="测试中…", fg=FG_MUTE)

        def work():
            import urllib.request
            base = h if h.startswith("http") else ("https://" + h if h else _core.API)
            try:
                req = urllib.request.Request(base + "/iptve.php?tid=ys&app=517",
                                             headers=_core.headers())
                txt = urllib.request.urlopen(req, timeout=10,
                                             context=_core._CTX).read().decode("utf-8", "ignore")
                n = txt.count("act=play")
                msg = "✓ 可用，返回 %d 个频道" % n if n else "✗ 响应异常"
                self.app.root.after(0, lambda: self.host_tip.configure(
                    text=msg, fg="#3ddc84" if n else BRAND))
            except Exception as e:
                self.app.root.after(0, lambda: self.host_tip.configure(
                    text="✗ 不可达：%s" % e, fg=BRAND))

        threading.Thread(target=work, daemon=True).start()

    def _reset(self):
        self.host_var.set("")
        self.ua_var.set("")
        self.buf_var.set("balanced")
        self.ttl_var.set("600")
        self.mpv_var.set("")

    def _save(self):
        obj = {
            "host": self.host_var.get().strip(),
            "ua": self.ua_var.get().strip(),
            "buffer": self.buf_var.get(),
            "ttl": self.ttl_var.get().strip() or "600",
            "mpv_extra": self.mpv_var.get().strip(),
        }
        _core.save_settings(self.app.settings_file, obj)
        self.app._reload_settings()
        self.app.mem_cache.clear()          # 域名/UA 变了，缓存作废
        self.app.status_lbl.configure(text="设置已保存")
        self.top.destroy()


def main():
    try:
        app = NativeApp()
        app.run()
    except Exception:
        try:
            import traceback
            logp = os.path.join(os.path.expanduser("~"), "Desktop", "cleantv_error.log")
            with open(logp, "w", encoding="utf-8") as f:
                f.write(traceback.format_exc())
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()

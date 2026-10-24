# -*- coding: utf-8 -*-
"""Windows 置顶 Pod（灵动岛风格悬浮胶囊）。

独立 Toplevel，照搬等待层 `overrideredirect + -topmost` 范式：无边框、
常驻屏幕右上角、始终在其他窗口之上。参考灵动岛的动态扩展形态：
  · idle  —— 正在播放：收窄成小胶囊（台标点 + 频道名）
  · busy  —— 切台/缓冲：横向展开 + 转圈 + 副标题（连接中/解析流…）
  · menu  —— 鼠标悬停时淡入快捷操作条：下一台 / 暂停▶ / 回主窗口

依赖 NativeApp 的：self._ui（线程安全）、self.cur（频道状态）、
self.root / 颜色常量 / F()。供电由 native_app 在关键节点调用：
  app.pod = FloatingPod(app)                      # 建（_setup 后）
  app.pod.show(chan, "连接中…")                    # busy
  app.pod.play(chan[, sub])                        # idle（播放）
  app.pod.hide() / app.pod.destroy()               # 收起/销毁
  app.pod.update_chan(name)                        # 仅改频道名
"""
import time
import tkinter as tk

# 与 native_app 一致的颜色（这里冗余定义，避免循环依赖；若失配不影响功能）
BG = "#0a0b0f"
BG2 = "#14161d"
FG = "#f2f4f8"
FG_DIM = "#a2a9b8"
GOLD = "#ffc531"

POD_H = 40                 # 胶囊常态高度
POD_R = 20                 # 圆角（画布用 PIL 做不到真圆角，用近似胶囊 + 纯色底）
PAD = 24                   # 距屏幕右上边距
LOGO_D = 22                # 台标点直径


def _badge_color(name):
    """台标点底色：无图时按频道名哈希给个品牌色（与列表徽标同思路）。"""
    if not name:
        return "#c0392b"
    palette = ("#c0392b", "#d35400", "#b8860b", "#1f8a5b", "#2563a8",
               "#6d3fb8", "#b8324a", "#0e7a8a")
    return palette[sum(map(ord, name)) % len(palette)]


class FloatingPod:
    def __init__(self, app, enabled=True):
        self.app = app
        self.enabled = enabled
        self.state = "hidden"          # hidden | idle | busy | menu
        self.chan = ""
        self.sub = ""
        self.menu_after = None
        self._hwnd = None
        self._name = ""
        self._draw_t0 = time.time()
        self._draw_job = None
        if not enabled:
            return
        self._build()

    # ------------------------------------------------------------------ 构建
    def _build(self):
        app = self.app
        self.win = tk.Toplevel(app.root)
        self.win.overrideredirect(True)
        try:
            self.win.attributes("-topmost", True)
        except Exception:
            pass
        self.win.configure(bg=BG2)
        # 胶囊外壳：用一个圆角近似容器（深底 + 描边）
        self.frame = tk.Frame(self.win, bg=BG2, highlightthickness=1,
                              highlightbackground="#3a3f4d")
        self.frame.pack(fill="both", expand=True)
        # 左：台标点（Canvas 画圆 / 显示首字）
        self.dot = tk.Canvas(self.frame, width=LOGO_D, height=LOGO_D,
                             bg=BG2, highlightthickness=0)
        self.dot.pack(side="left", padx=(10, 0))
        # 中：频道名
        self.name_lbl = tk.Label(self.frame, text="", bg=BG2, fg=FG,
                                 font=app.F(11, True), padx=8)
        self.name_lbl.pack(side="left")
        # 右：副标题（busy 时可见）
        self.sub_lbl = tk.Label(self.frame, text="", bg=BG2, fg=FG_DIM,
                                font=app.F(9))
        self.sub_lbl.pack(side="left", padx=(0, 16))
        # 转圈（busy 时用 Canvas 画弧线，_tick 驱动）
        self.spin = tk.Canvas(self.frame, width=16, height=16, bg=BG2,
                              highlightthickness=0)
        self.spin.pack(side="left", padx=(0, 10))
        # hover 菜单条（默认隐藏）
        self.menu = tk.Frame(self.frame, bg=BG2)
        self._mk_btn("▶", "暂停/播放", self._on_toggle_pause)
        self._mk_btn("→", "下一台", self._on_next)
        self._mk_btn("⌂", "回主窗口", self._on_focus)
        # 事件：hover 展开菜单，离开收起
        for w in (self.win, self.frame, self.dot, self.name_lbl):
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)
        self.win.withdraw()          # 初始隐藏

    def _mk_btn(self, text, tip, cb):
        b = tk.Button(self.menu, text=text, bg=BG2, fg=FG, bd=0,
                      activebackground="#2a2e3a", activeforeground=FG,
                      font=self.app.F(10, True), padx=8, pady=2,
                      command=cb, cursor="hand2")
        b.pack(side="left", padx=2)
        return b

    # ------------------------------------------------------------------ 定位
    def _place(self):
        try:
            w = self.win.winfo_reqwidth()
            h = self.win.winfo_reqheight()
            sw = self.win.winfo_screenwidth()
            self.win.geometry("%dx%d+%d+%d" % (w, h, sw - w - PAD, PAD))
            # 圆角近似：PIL 截图后贴圆角太重，这里用深色底 + 描边即可
        except Exception:
            pass

    # ------------------------------------------------------------------ 状态切换
    def show(self, chan, sub=""):
        """busy：切台/缓冲，动态展开。"""
        if not self.enabled:
            return
        self.chan = chan or self.chan
        self.sub = sub
        self._set_dot(chan)
        self.name_lbl.configure(text=self._ellipsis(chan))
        self.sub_lbl.configure(text=("· %s" % sub) if sub else "")
        self._show_menu(False)
        self.state = "busy"
        self._start_spin()
        self.win.deiconify()
        try:
            self.win.attributes("-topmost", True)
        except Exception:
            pass
        self._place()
        self.win.lift()

    def play(self, chan=None, sub=""):
        """idle：正在播放，常态收窄。"""
        if not self.enabled:
            return
        if chan is not None:
            self.chan = chan
        self._set_dot(self.chan)
        self.name_lbl.configure(text=self._ellipsis(self.chan))
        self.sub_lbl.configure(text=("· %s" % sub) if sub else "")
        self.state = "idle"
        self._stop_spin()
        self.win.deiconify()
        try:
            self.win.attributes("-topmost", True)
        except Exception:
            pass
        self._place()
        self.win.lift()

    def update_chan(self, name):
        self.chan = name
        if self.state in ("idle", "busy"):
            self.play(name)

    def hide(self):
        if not self.enabled:
            return
        self._show_menu(False)
        self._stop_spin()
        self.state = "hidden"
        try:
            self.win.withdraw()
        except Exception:
            pass

    def destroy(self):
        if not self.enabled:
            return
        self._stop_spin()
        try:
            self.win.destroy()
        except Exception:
            pass
        self.enabled = False

    # ------------------------------------------------------------------ 台标点
    def _set_dot(self, name):
        self.dot.delete("all")
        color = _badge_color(name)
        # 试台标库（有图贴图，无图就画首字徽标）
        url = None
        try:
            url = self.app.pod_match_logo(name)
        except Exception:
            url = None
        self._paint_dot_char((name or "?")[:1].upper(), color)
        if url:
            self._dot_gen = getattr(self, "_dot_gen", 0) + 1
            gen = self._dot_gen
            self.app.pod_request_logo(url, lambda img, gen=gen, name=name:
                                      self._on_logo(img, gen, name))

    def _paint_dot_char(self, ch, color):
        self.dot.delete("all")
        self.dot.create_oval(0, 0, LOGO_D, LOGO_D, fill=color, outline="")
        self.dot.create_text(LOGO_D // 2, LOGO_D // 2, text=ch or "?",
                             fill="#fff", font=self.app.F(9, True))

    def _on_logo(self, img, gen, name):
        # 台标下载回来：贴上（换台了/销毁了则丢弃）
        if not self.enabled or gen != getattr(self, "_dot_gen", -1) \
                or name != self.chan:
            return
        if img is None:
            return
        try:
            self.dot.delete("all")
            self.dot.create_image(LOGO_D // 2, LOGO_D // 2, image=img)
            self.dot.image = img
        except Exception:
            pass

    # ------------------------------------------------------------------ 转圈
    def _start_spin(self):
        self._draw_t0 = time.time()
        if self._draw_job is None:
            self._draw_job = self.app.root.after(50, self._tick)

    def _stop_spin(self):
        self.spin.delete("all")

    def _tick(self):
        self._draw_job = None
        if self.state != "busy":
            return
        t = int((time.time() - self._draw_t0) * 8) % 8
        self.spin.delete("all")
        # 8 段弧线，逐段点亮（转圈）
        for i in range(8):
            color = FG if i == t else "#3a3f4d"
            self.spin.create_arc(1, 1, 15, 15, start=i * 45, extent=35,
                                 style="arc", outline=color, width=2)
        self._draw_job = self.app.root.after(50, self._tick)

    # ------------------------------------------------------------------ hover 菜单
    def _on_enter(self, e=None):
        if self.state not in ("idle", "busy"):
            return
        if self.menu_after:
            self.app.root.after_cancel(self.menu_after)
            self.menu_after = None
        self._show_menu(True)

    def _on_leave(self, e=None):
        if self.menu_after:
            self.app.root.after_cancel(self.menu_after)
        self.menu_after = self.app.root.after(600, self._hide_menu_if_away)

    def _hide_menu_if_away(self):
        self.menu_after = None
        if self._pointer_inside():
            return
        self._show_menu(False)

    def _pointer_inside(self):
        try:
            x = self.win.winfo_pointerx()
            y = self.win.winfo_pointery()
            gx, gy = self.win.winfo_rootx(), self.win.winfo_rooty()
            w, h = self.win.winfo_width(), self.win.winfo_height()
            return gx <= x <= gx + w and gy <= y <= gy + h
        except Exception:
            return False

    def _show_menu(self, on):
        if on and self.state != "menu":
            self.state = "menu"
            self.menu.pack(side="left", before=self.spin, padx=(4, 4))
            self._place()
        elif not on and self.state == "menu":
            self.state = "idle" if self.state == "menu" else self.state
            self.menu.pack_forget()
            self._place()

    # ------------------------------------------------------------------ 动作
    def _on_toggle_pause(self):
        try:
            self.app._toggle_pause()
        except Exception:
            pass

    def _on_next(self):
        try:
            self.app._next_channel()
        except Exception:
            pass

    def _on_focus(self):
        try:
            self.app._restore_main()
        except Exception:
            pass

    # ------------------------------------------------------------------ 工具
    def _ellipsis(self, name, width=9):
        s = name or ""
        if len(s) > width:
            return s[: width - 1] + "…"
        return s
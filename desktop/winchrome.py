# -*- coding: utf-8 -*-
"""沉浸式窗口外框（Windows）：去掉系统标题栏，保留系统原生缩放边。

为什么这么写 —— 结论都来自 tools/_probe_variantA.py 的实测（不是猜的）：

  ① 摘掉 WS_CAPTION，保留 WS_THICKFRAME，**完全不动 WM_NCCALCSIZE**。
     系统于是自己算出「客户区 = 窗口 − 一圈缩放边」，标题栏就没了。缩放于是
     完全由系统负责：命中测试原生返回 HTLEFT / HTTOP / HTBOTTOMLEFT…，
     鼠标指针形状、贴边吸附（Aero Snap）、Win+方向键 全部免费到手，我们
     一行 hit-test 代码都不用写。

     ★ 千万别为了「客户区铺满整窗」去把 WM_NCCALCSIZE 返回 0。实测（同探针
       A2 段）：客户的区一旦铺满整窗，系统的非客户区命中测试**全部退化成
       HTCLIENT**，缩放边当场失效 —— 因为命中测试是拿「窗口几何 − 客户区」
       现算的，不是按窗口样式度量算的。留着那圈边框，一切都原生。

  ② WM_GETMINMAXINFO 里按边框宽度补偿最大化矩形。
     无 caption 的窗口默认最大化 = 铺满整个显示器（会盖住任务栏）。规律是
     「窗口矩形 = ptMaxSize 摆在 ptMaxPosition」，把 ptMaxPosition 往里收
     一圈边框、ptMaxSize 再放大两圈边框，客户区就正好 == 工作区
     （实测 1920×1020，任务栏完好）。

     边框宽度 frame_inset() 是运行时量的（ClientToScreen − GetWindowRect），
     实测 8px。**不能**按 GetSystemMetrics(SM_CXSIZEFRAME)(4) +
     SM_CXPADDEDBORDER(5) = 9 算，差 1px。

  ③ 那圈 8px 缩放边默认是系统浅灰，要刷成 App 底色。★ 开了 DWM 合成之后非
     客户区是 **DWM** 画的，WM_NCPAINT 里 FillRect 盖不住它（实测边框像素仍是
     #cecece）；只能走 DWM 属性：DWMWA_BORDER_COLOR(34) → USE_IMMERSIVE_DARK_
     MODE(20/19)，见 _paint_border。WM_NCPAINT 那份留着当无 DWM 时的兜底。

  拖动？没有标题栏就没有 HTCAPTION 区域，所以由 App 侧绑鼠标
  （见 native_app._bind_drag）：在装饰区按下 → start_drag() →
  PostMessage(WM_NCLBUTTONDOWN, HTCAPTION) 交给系统的窗口移动循环。
  ★ 这里必须 PostMessage 不能 SendMessage —— 移动循环是模态的，SendMessage
    会把发起它的 Tk 回调堵死，连 ButtonRelease 都收不到。

  ★ Tk 的 -fullscreen 往返会**销毁并重建**顶层窗口（HWND 变了、style 归零），
    外框样式会一起丢 → 必须每次 <Configure> 调一次 ensure() 重新挂上。
"""

import ctypes
import sys

IS_WIN = sys.platform.startswith("win")

available = False

if IS_WIN:
    from ctypes import wintypes

    GWL_STYLE, GWLP_WNDPROC = -16, -4
    WS_CAPTION = 0x00C00000
    WS_THICKFRAME = 0x00040000

    SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER = 0x0001, 0x0002, 0x0004
    SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x0010, 0x0020
    SWP_FLAGS = (SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER
                 | SWP_NOACTIVATE | SWP_FRAMECHANGED)

    WM_GETMINMAXINFO = 0x0024
    WM_NCPAINT = 0x0085
    WM_NCLBUTTONDOWN = 0x00A1
    HTCAPTION = 2

    SW_SHOWNORMAL, SW_SHOWMINIMIZED, SW_SHOWMAXIMIZED = 1, 2, 3
    SW_RESTORE = 9

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)

    _u = ctypes.windll.user32
    _gdi = ctypes.windll.gdi32

    _u.SetWindowLongPtrW.restype = ctypes.c_void_p
    _u.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    _u.GetWindowLongPtrW.restype = ctypes.c_void_p
    _u.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    _u.CallWindowProcW.restype = LRESULT
    _u.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                                   wintypes.WPARAM, wintypes.LPARAM]
    _u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]
    _u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    _u.GetParent.restype = wintypes.HWND
    _u.GetParent.argtypes = [wintypes.HWND]
    _u.MonitorFromWindow.restype = ctypes.c_void_p
    _u.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    _u.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _u.GetWindowDC.restype = wintypes.HDC
    _u.GetWindowDC.argtypes = [wintypes.HWND]
    _u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    _u.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                            wintypes.HBRUSH]
    _gdi.CreateSolidBrush.restype = wintypes.HBRUSH
    _gdi.CreateSolidBrush.argtypes = [wintypes.COLORREF]
    _gdi.DeleteObject.argtypes = [wintypes.HGDIOBJ]

    class _MINMAXINFO(ctypes.Structure):
        _fields_ = [("ptReserved", wintypes.POINT),
                    ("ptMaxSize", wintypes.POINT),
                    ("ptMaxPosition", wintypes.POINT),
                    ("ptMinTrackSize", wintypes.POINT),
                    ("ptMaxTrackSize", wintypes.POINT)]

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD),
                    ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]

    def _monitor(hwnd):
        """返回 hwnd 所在（最近）显示器的 MONITORINFO，失败 None。"""
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        mon = _u.MonitorFromWindow(hwnd, 2)         # MONITOR_DEFAULTTONEAREST
        if not mon or not _u.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return None
        return mi

    def work_area(hwnd):
        mi = _monitor(hwnd)
        if not mi:
            return None
        w = mi.rcWork
        return (w.left, w.top, w.right, w.bottom)

    def screen_rect(hwnd):
        mi = _monitor(hwnd)
        if not mi:
            return None
        m = mi.rcMonitor
        return (m.left, m.top, m.right, m.bottom)

    def top_hwnd(child):
        """Tk 的 winfo_id() 是内层子窗口 —— 一路往上找到真正的顶层 HWND。"""
        h = child
        for _ in range(8):
            p = _u.GetParent(h)
            if not p:
                break
            h = p
        return h

    def class_name(hwnd):
        buf = ctypes.create_unicode_buffer(256)
        try:
            _u.GetClassNameW(hwnd, buf, 256)
        except Exception:
            pass
        return buf.value

    def frame_inset(hwnd):
        """客户区相对窗口左上角的内缩（= 那圈缩放边的宽度）。

        实测 8px；窗口最大化时量出来同样是 8（窗口 rect 与客户区一起内缩），
        所以不用特判最大化态。
        """
        r = wintypes.RECT()
        if not _u.GetWindowRect(hwnd, ctypes.byref(r)):
            return (8, 8)
        o = wintypes.POINT(0, 0)
        if not _u.ClientToScreen(hwnd, ctypes.byref(o)):
            return (8, 8)
        return (o.x - r.left, o.y - r.top)

    def _colorref(spec):
        """'#rrggbb' → COLORREF(0x00BBGGRR)。"""
        s = spec.lstrip("#")
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
        return r | (g << 8) | (b << 16)

    class ImmersiveFrame:
        """把 Tk 顶层窗口变成「无系统标题栏 + 原生缩放边」的沉浸式窗口。"""

        def __init__(self, root, frame_color="#0a0b0f"):
            self.root = root
            self.frame_color = frame_color
            self._hwnd = 0
            self._child = 0
            self._proc = None        # WNDPROC 引用，必须留，否则被 GC → 崩溃
            self._old = None         # 被替换掉的窗口过程
            self._inset = (8, 8)
            self._available = True

        # ---------------- 挂载 / 重挂 / 卸下 ----------------
        def attach(self):
            """给顶层窗口装上无标题栏样式 + 子类化窗口过程。失败返回 False。"""
            if not self._available:
                return False
            try:
                child = self.root.winfo_id()
                if not child:
                    return False
                h = top_hwnd(child)
                if not h:
                    return False
                style = int(_u.GetWindowLongPtrW(h, GWL_STYLE) or 0)
                if not style:               # 窗口还没建利索，等下一轮
                    return False
            except Exception:
                return False
            self._child = child
            self._hwnd = h
            self._proc = WNDPROC(self._wndproc)
            self._old = _u.SetWindowLongPtrW(
                h, GWLP_WNDPROC, ctypes.cast(self._proc, ctypes.c_void_p))
            self._apply_style(h)
            self._paint_border(h)
            self._measure()
            return True

        def ensure(self):
            """每次 <Configure> 调一遍：HWND 换了就重挂；caption 被加回来再摘。

            ★ Tk 的 -fullscreen 往返会重建顶层窗口，而且是**保住内层子窗口、把它
              重新挂到一个新的顶层**——winfo_id() 不变，变的是它的 parent。所以
              必须同时比对 winfo_id 和 top_hwnd，只比 winfo_id 会漏判（实测踩过）。
            """
            if not self._available or not self._hwnd:
                return False
            try:
                child = self.root.winfo_id()
            except Exception:
                return False
            if not child:
                return False
            if child != self._child or top_hwnd(child) != self._hwnd:
                self._old = None            # 旧窗口已销毁，别再去还原它
                return self.attach()
            try:
                style = int(_u.GetWindowLongPtrW(self._hwnd, GWL_STYLE) or 0)
            except Exception:
                return False
            if style & WS_CAPTION:
                self._apply_style(self._hwnd)
                self._paint_border(self._hwnd)
            self._measure()
            return True

        def detach(self):
            """把窗口过程还给 Tk（销毁窗口前必须调，否则回调进已回收的对象）。"""
            try:
                if self._hwnd and self._old:
                    _u.SetWindowLongPtrW(
                        self._hwnd, GWLP_WNDPROC,
                        ctypes.cast(self._old, ctypes.c_void_p))
            except Exception:
                pass
            self._hwnd = 0
            self._child = 0
            self._old = None
            self._proc = None

        # ---------------- 内部 ----------------
        def _apply_style(self, h):
            try:
                style = int(_u.GetWindowLongPtrW(h, GWL_STYLE) or 0)
                style = (style & ~WS_CAPTION) | WS_THICKFRAME
                _u.SetWindowLongPtrW(h, GWL_STYLE, style)
                _u.SetWindowPos(h, None, 0, 0, 0, 0, SWP_FLAGS)
            except Exception:
                pass

        def _measure(self):
            """量一次边框宽度（最大化态量出来的也是同一值，但仍避开最大化时量）。"""
            if not self._hwnd:
                return
            try:
                if not _u.IsZoomed(self._hwnd):
                    ix, iy = frame_inset(self._hwnd)
                    if 0 <= ix <= 64 and 0 <= iy <= 64:
                        self._inset = (ix, iy)
            except Exception:
                pass

        def _wndproc(self, h, msg, wparam, lparam):
            try:
                if msg == WM_GETMINMAXINFO:
                    r = _u.CallWindowProcW(self._old, h, msg, wparam, lparam)
                    self._fix_minmax(h, lparam)     # 先让 Tk 设好最小尺寸，再改
                    return r
                if msg == WM_NCPAINT:
                    self._paint_frame(h)
                    return 0
            except Exception:
                pass
            return _u.CallWindowProcW(self._old, h, msg, wparam, lparam)

        def _fix_minmax(self, h, lparam):
            """无 caption 窗口默认最大化会铺满整屏（盖任务栏）→ 按边框补偿。"""
            wk = work_area(h)
            mo = screen_rect(h)
            if not wk or not mo:
                return
            ix, iy = self._inset
            mmi = ctypes.cast(lparam, ctypes.POINTER(_MINMAXINFO)).contents
            ww, wh = wk[2] - wk[0], wk[3] - wk[1]
            # 窗口矩形 = ptMaxSize 摆在 ptMaxPosition（坐标相对显示器左上角）
            mmi.ptMaxPosition.x = wk[0] - mo[0] - ix
            mmi.ptMaxPosition.y = wk[1] - mo[1] - iy
            mmi.ptMaxSize.x = ww + 2 * ix
            mmi.ptMaxSize.y = wh + 2 * iy

        def _paint_border(self, h):
            """让那圈 8px 缩放边变成 App 底色 —— 必须走 DWM，不是 WM_NCPAINT。

            ★ 开了 DWM 合成之后非客户区是 **DWM** 画的，WM_NCPAINT 里 FillRect
              根本盖不住它：实测窗口边框像素是 #cecece（系统浅灰），而我们刷的
              颜色一点没露出来。所以只能设 DWM 窗口属性：
                34 = DWMWA_BORDER_COLOR（Win11 22000+，直接给 COLORREF）
                20 = DWMWA_USE_IMMERSIVE_DARK_MODE（20H1+）
                19 = 同上（20H1 之前的老号）
              能设上哪个算哪个，设不上就维持系统色（不影响功能）。
            """
            try:
                dwm = ctypes.windll.dwmapi
            except Exception:
                return False
            ok = False
            # ★ 别因为某个属性返回 S_OK 就提前返回 —— 实测 Win10 上 34 会
            #   「成功但不生效」，20/19 才是真的把边框压暗的那个。全都设一遍。
            for attr in (34, 20, 19):
                try:
                    val = wintypes.DWORD(_colorref(self.frame_color))
                    if dwm.DwmSetWindowAttribute(h, attr, ctypes.byref(val),
                                                 4) == 0:
                        ok = True
                except Exception:
                    continue
            return ok

        def _paint_frame(self, h):
            """非 DWM 时的兜底：把那圈缩放边刷成 App 底色。

            （有 DWM 时真正生效的是 _paint_border；这份留着不影响正确性。）
            """
            # 1) 先让原窗口过程画一遍 —— 保证非客户区被 validate，否则
            #    WM_NCPAINT 会反复来（ValidateRect 没做）。
            try:
                _u.CallWindowProcW(self._old, h, WM_NCPAINT, 1, 0)
            except Exception:
                pass
            # 2) 再把四边盖成 App 底色
            try:
                ix, iy = self._inset
                if ix <= 0 and iy <= 0:
                    return
                hdc = _u.GetWindowDC(h)
                if not hdc:
                    return
                r = wintypes.RECT()
                _u.GetWindowRect(h, ctypes.byref(r))
                W, H = r.right - r.left, r.bottom - r.top
                br = _gdi.CreateSolidBrush(_colorref(self.frame_color))
                try:
                    for box in ((0, 0, W, iy),              # 上
                                (0, H - iy, W, H),          # 下
                                (0, 0, ix, H),              # 左
                                (W - ix, 0, W, H)):         # 右
                        rc = wintypes.RECT(*box)
                        _u.FillRect(hdc, ctypes.byref(rc), br)
                finally:
                    _gdi.DeleteObject(br)
                    _u.ReleaseDC(h, hdc)
            except Exception:
                pass

        # ---------------- 对外操作 ----------------
        @property
        def maximized(self):
            try:
                return bool(self._hwnd and _u.IsZoomed(self._hwnd))
            except Exception:
                return False

        @property
        def hwnd(self):
            return self._hwnd

        @property
        def child(self):
            return self._child

        @property
        def inset(self):
            return self._inset

        def maximize(self):
            if self._hwnd:
                _u.ShowWindow(self._hwnd, SW_SHOWMAXIMIZED)

        def minimize(self):
            if self._hwnd:
                _u.ShowWindow(self._hwnd, SW_SHOWMINIMIZED)

        def restore(self):
            if self._hwnd:
                _u.ShowWindow(self._hwnd, SW_RESTORE)

        def toggle_maximize(self):
            if not self._hwnd:
                return
            try:
                if _u.IsZoomed(self._hwnd):
                    _u.ShowWindow(self._hwnd, SW_RESTORE)
                else:
                    _u.ShowWindow(self._hwnd, SW_SHOWMAXIMIZED)
            except Exception:
                pass

        def start_drag(self):
            """装饰区按下左键 → 交给系统窗口移动循环（手感与原生标题栏一致）。"""
            h = self._hwnd
            if not h:
                return
            try:
                pt = wintypes.POINT()
                _u.GetCursorPos(ctypes.byref(pt))
                _u.ReleaseCapture()
                # ★ lParam 必须是**当前光标坐标**。传 0 的话 DefWindowProc 会走
                #   「键盘移动」分支（箭头键搬窗口、鼠标一点击就结束）——表现为
                #   拖不动窗口，且把紧接着的第二次点击吃掉（双击最大化再也收不到）。
                lp = ((pt.y & 0xFFFF) << 16) | (pt.x & 0xFFFF)
                # PostMessage（非 SendMessage）：移动循环是模态的，Send 会把
                # 发起它的 Tk 回调堵死。
                _u.PostMessageW(h, WM_NCLBUTTONDOWN, HTCAPTION, lp)
            except Exception:
                pass

else:                                   # 非 Windows：空实现，调用方无感
    def work_area(hwnd):
        return None

    def screen_rect(hwnd):
        return None

    def top_hwnd(child):
        return child

    def class_name(hwnd):
        return ""

    def frame_inset(hwnd):
        return (0, 0)

    class ImmersiveFrame:
        def __init__(self, root, frame_color="#000000"):
            self.root = root
            self.frame_color = frame_color
            self._available = False

        @property
        def maximized(self):
            return False

        @property
        def hwnd(self):
            return 0

        def attach(self):
            return False

        def ensure(self):
            return False

        def detach(self):
            pass

        def maximize(self):
            pass

        def minimize(self):
            pass

        def restore(self):
            pass

        def toggle_maximize(self):
            pass

        def start_drag(self):
            pass


available = IS_WIN

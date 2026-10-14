# -*- coding: utf-8 -*-
"""
纯净电视 · 官网源「内嵌浏览器」播放器（Desktop）· WebView2 版
================================================================
官网源里 1100 多条线路大部分是**官方直播网页**（CCTV、卫视官网、芒果 TV…）。
这些页面用 WASM + MSE 播 `blob:` 源，解析出来的地址 mpv/ExoPlayer 都播不了，
所以必须真浏览器渲染 —— 把官方页面嵌进我们自己的播放区里播。

历史（为什么换成 WebView2）：
    v2.3.x 用「启动完整 Edge 进程 --app=… → 全屏幕搜它的窗口 → SetParent 强改成
    子窗口」。这是在 hack 一个**没打算被嵌入**的独立程序，于是：
      · 标题栏画在客户区里、Windows 报非客户区顶 0 → 只能按 DPI 瞎猜；
      · Chromium 按网址记窗口位置（app_window_placement）跟 --window-size 打架；
      · 页面自己 resizeTo 跟尺寸看门狗对抗；
      · 换台并发时进程互杀，漏出去的窗口变成桌面上的游离浏览器（v2.3.5 根因）。
    每个问题都是在跟 Chromium 抢窗口控制权，永远修不完 —— 一步一个坎。

    现在换成 **WebView2**：微软官方「嵌进你自己程序」的浏览器控件
    （Windows 系统组件，Office / Outlook / 微信小程序 PC 版都在用）。
    控件生来就是主窗口的子窗口：
      · 没有「找窗口 / SetParent / 抢边框」这三步 —— 这一类 bug 整体消失；
      · 尺寸用 put_Bounds 精确控制，页面无权改窗口 —— 没有对抗；
      · UA 用 Settings2.put_UserAgent 直接换（连浏览器都不用重启）；
      · 脚本用 AddScriptToExecuteOnDocumentCreated 官方预注入（比 CDP 抢时机更稳）；
      · 生命周期由主程序管理，没有游离进程可言；
      · Win10/11 系统自带运行时（缺失时 find_browser()/webview2_available() 报不可用，
        native_app 走 mpv 嗅探退路）。

实现说明（纯 ctypes，零重依赖）：
    只用 SDK 里的 `WebView2Loader.dll`（微软官方可再分发，约 190KB，随 exe 打包）
    调 `CreateCoreWebView2EnvironmentWithOptions`，其余全部走 COM vtable。
    接口序号 / GUID 均从官方 WebView2.h（SDK 1.0.4258.31）逐一核对：
      ICoreWebView2Environment        b96d755e-0319-4e92-a296-23436f46a1fc
        vtbl 3 = CreateCoreWebView2Controller(parent, handler)
      ICoreWebView2Controller         4d00c0d1-9434-4eb6-8078-8697a560334f
        vtbl 4 = put_IsVisible  6 = put_Bounds  23 = NotifyParentWindowPositionChanged
        24 = Close  25 = get_CoreWebView2
      ICoreWebView2                   76eceacb-0462-4d94-ac83-423a6793775e
        vtbl 3 = get_Settings  5 = Navigate  27/28 = Add/Remove…OnDocumentCreated
        29 = ExecuteScript
      ICoreWebView2Settings2          ee9a0f68-f46c-4e32-ac23-ef8cac224d2a
        vtbl 22 = put_UserAgent（Settings 九对属性占 3..20，21/22 是 UserAgent）

线程模型（★ 全项目最重要的一段 —— 四条铁律全部实测验证）：
    WebView2 的 COM 对象线程亲和极强，以下是拿隔离实验换来的铁律：
      1. **专职 STA 线程**：内部起一条 daemon 线程，CoInitializeEx(APARTMENTTHREADED)
         后跑 MsgWaitForMultipleObjects 消息泵。env/controller/core 全部在这条线程
         创建和调用，别的线程碰 vtable 必报 0x802A000C「只能从创建对象的线程调用」
         或直接段错误。
      2. **泵必须先跑起来再 create**：官方文档「must be created on a UI thread
         that uses a message pump」是字面意思 —— 泵没转就调 create，环境会被挂到
         loader 内部线程上，之后满屏诡异错误（这就是 v2.3.6 一整天的坑）。
         所以 create 也是作为任务投进运行中的泵。
      3. **回调只存指针**：completion 回调（ctypes 回调栈）里只做 AddRef + 存指针 +
         set Event，COM 调用一律留给泵循环体的普通 Python 帧。
      4. **等待方必须泵消息**：play()/close() 等待结果时，若在创建者线程（Tk 主线程）
         上等，必须边泵消息边等（PeekMessage 循环）—— WebView2 会向 parent 窗口
         所属线程（主线程）发同步消息，主线程不泵就死锁（实测卡死在 COM 调用里）。
         Tk mainloop() 天然泵消息，所以后台线程等 Event、主线程跑 mainloop 最稳。
    作业（play/eval/…）是生成器，每步可 yield 等待秒数；代次变了立刻作废，
    换台并发绝不互杀 —— 控件是大家的，根本没有「进程互杀」这回事。
"""

import os
import sys
import json
import time
import atexit
import queue
import ctypes
import threading
from ctypes import (wintypes, c_void_p, c_wchar_p, c_ulong, c_longlong,
                    POINTER, Structure, WINFUNCTYPE, cast, byref, c_ubyte)

IS_WIN = sys.platform.startswith("win")

# ---------------------------------------------------------------- 基础常量
def _hresult(v):
    """把 32 位 HRESULT 折回有符号。"""
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v >= 0x80000000 else v

S_OK = _hresult(0x00000000)
S_FALSE = _hresult(0x00000001)
E_NOINTERFACE = _hresult(0x80004002)
E_FAIL = _hresult(0x80004005)
RPC_E_CHANGED_MODE = _hresult(0x80010106)
COINIT_APARTMENTTHREADED = 0x2
QS_ALLINPUT = 0x04FF
WM_NULL = 0x0000

MOBILE_UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")
PC_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 额外浏览器参数：官方页面要自动起播（不然 Chrome 策略拦 autoplay），
# 关掉翻译/投递悬浮这类干扰。
# ★ --disable-gpu-sandbox 是实测必须的：部分机器（云桌面/虚拟 GPU/某些驱动环境）
#   上 GPU 进程的沙箱起不来（exit_code=7 连崩 9 次），Chromium 直接
#   「GPU process isn't usable. Goodbye」整个自杀 —— 表现为控件全黑/窗口树
#   隐藏/浏览器进程十几秒后消失。只禁 GPU 沙箱、保留硬件加速，实测稳定。
#   （对照组实测：--disable-gpu / --use-angle=swiftshader 都救不了，
#     --disable-gpu-sandbox 与 --no-sandbox 都能救，取前者。）
# ★ 防后台节流三件套 + CalculateNativeWinOcclusion（v2.10.42 实测）：沉浸式
#   无边框主窗里 WebView2 会被 Chromium 误判「被遮挡/后台」，页面 SPA 的
#   定时器被节流 —— gdtv 页 50s 内连 <video> 都不建；关掉后 46s 能播到
#   first frame。与 cdp.py 嗅探浏览器用同一组旗标。
DEFAULT_EXTRA_ARGS = ("--autoplay-policy=no-user-gesture-required "
                      "--disable-features=Translate,MediaRouter,"
                      "CalculateNativeWinOcclusion "
                      "--disable-background-timer-throttling "
                      "--disable-backgrounding-occluded-windows "
                      "--disable-renderer-backgrounding "
                      "--disable-gpu-sandbox")
# 允许环境变量整体覆盖（诊断实验 / 用户自定义参数）。设为空串 = 不带任何参数。
EXTRA_ARGS = os.environ.get("WVT_EMBED_ARGS", DEFAULT_EXTRA_ARGS)
# 兼容目标：只用 v0 接口 + Settings2(UserAgent) + AddScriptOnDocumentCreated，
# 把门槛压到这些特性的最低 SDK 版，老一点的常青版运行时也能用。
TARGET_COMPAT = "1.0.992.28"

# ---------------------------------------------------------------- GUID
class GUID(Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", c_ubyte * 8)]

def _guid(s):
    s = s.replace("-", "")
    g = GUID()
    g.Data1 = int(s[0:8], 16)
    g.Data2 = int(s[8:12], 16)
    g.Data3 = int(s[12:16], 16)
    for i in range(8):
        g.Data4[i] = int(s[16 + i * 2:18 + i * 2], 16)
    return g

def _guid_eq(a, b):
    return (a.Data1 == b.Data1 and a.Data2 == b.Data2 and a.Data3 == b.Data3
            and bytes(a.Data4) == bytes(b.Data4))

IID_IUNKNOWN = _guid("00000000-0000-0000-C000-000000000046")
IID_ENV_OPTIONS = _guid("2fde08a8-1e9a-4766-8c05-95a9ceb9d1c5")
IID_ENV_HANDLER = _guid("4e8a3389-c9d8-4bd2-b6b5-124fee6cc14d")
IID_CTRL_HANDLER = _guid("6c4819f3-c9b7-4260-8127-c9f5bde7f68c")
IID_EXEC_HANDLER = _guid("49511172-cc67-4bca-9923-137112f4c4cc")
IID_ADDSCRIPT_HANDLER = _guid("b99369f3-9b11-47b5-bc6f-8e7895fcea17")
IID_SETTINGS2 = _guid("ee9a0f68-f46c-4e32-ac23-ef8cac224d2a")
IID_NEWWIN_HANDLER = _guid("d4c185fe-c81c-4989-97af-2d3f-a7ab5651")

# ---------------------------------------------------------------- vtable 调用
PVOID = c_void_p
FN_QI = WINFUNCTYPE(ctypes.HRESULT, PVOID, POINTER(GUID), POINTER(PVOID))
FN_REFCOUNT = WINFUNCTYPE(c_ulong, PVOID)

def _vtbl_call(obj, index, fn_type, *args):
    """对裸 COM 接口指针 obj 调 vtable 第 index 个方法（obj 是地址值）。"""
    obj = c_void_p(obj.value if isinstance(obj, c_void_p) else obj)
    vtbl = cast(obj, POINTER(c_void_p)).contents.value
    fn_addr = cast(cast(vtbl, POINTER(c_void_p))[index], c_void_p).value
    fn = cast(fn_addr, fn_type)
    return fn(obj, *args)

def _cotask_wstr(s, out=None):
    """
    get_* 的 [out] LPWSTR* 要求我们用 CoTaskMemAlloc 分配（调用方释放）。
    ★ 分配完必须把指针写进 out 参数 —— 否则 loader 读到的是空指针/垃圾。
    """
    ole32 = getattr(_cotask_wstr, "_ole32", None)
    if ole32 is None:
        ole32 = ctypes.windll.ole32
        # ★ 必须显式 restype=c_void_p：默认按 c_int 返回会把 64 位指针截断。
        ole32.CoTaskMemAlloc.restype = c_void_p
        _cotask_wstr._ole32 = ole32
    n = (len(s) + 1) * 2
    p = ole32.CoTaskMemAlloc(n)
    if not p:
        return E_FAIL
    ctypes.memmove(p, ctypes.create_unicode_buffer(s), n)
    if out is not None:
        try:
            out[0] = p
        except Exception:
            return E_FAIL
    return S_OK

class ComObject:
    """
    最小的进程内 COM 对象（只做回调用）：QI 只认自己的 IID（可给多个，继承链）
    和 IUnknown，引用计数只增不减（数量个位数，进程退出即释放，不值得做真回收）。
    布局：接口指针 → [vtbl_addr] → vtable 数组（前 3 项固定 QI/AddRef/Release）。
    """

    def __init__(self, iids, entries):
        self._iids = iids if isinstance(iids, (list, tuple)) else [iids]
        self._refs = 1
        fns = [FN_QI(self._qi), FN_REFCOUNT(self._addref), FN_REFCOUNT(self._release)]
        for ft, impl in entries:
            fns.append(ft(impl))
        self._keep = fns                      # 防 GC —— vtable 里的地址必须一直有效
        self._vt = (c_void_p * len(fns))(*[cast(f, c_void_p) for f in fns])
        # 接口指针指向「装着 vtable 地址的槽」，不是 vtable 本身
        self._obj = (c_void_p * 1)(cast(self._vt, c_void_p).value)

    def ptr(self):
        return cast(self._obj, c_void_p)

    def _qi(self, this, riid, out):
        try:
            g = riid.contents
            if (_guid_eq(g, IID_IUNKNOWN)
                    or any(_guid_eq(g, i) for i in self._iids)):
                out[0] = this
                return S_OK
        except Exception:
            pass
        out[0] = None
        return E_NOINTERFACE

    def _addref(self, this):
        self._refs += 1
        return self._refs

    def _release(self, this):
        self._refs -= 1
        return max(1, self._refs)             # 永不真释放，见 __init__ 注释


# 保活的 COM 回调对象：(入库时间, obj)。定时清理，防每秒轮询把列表撑大。
_LIVE_HANDLERS = []

def _mk_handler(iid, fn):
    """构造一个「Invoke(errorCode, arg)」形状的完成回调（返回 ComObject）。"""
    FT = WINFUNCTYPE(ctypes.HRESULT, PVOID, ctypes.HRESULT, c_void_p)

    def invoke(this, err, arg):
        try:
            if iid is IID_EXEC_HANDLER or iid is IID_ADDSCRIPT_HANDLER:
                arg = (cast(arg, c_wchar_p).value if arg else None)
            fn(err, arg)
        except Exception:
            pass
        return S_OK

    obj = ComObject(iid, [(FT, invoke)])
    # ★ 调用方必须持有返回的 obj 直到回调确认交付；这里再挂一份带时间戳的
    #   保活（_prune_handlers 定期清理 120 秒前的旧回调），双保险防 GC 悬空。
    _LIVE_HANDLERS.append((time.time(), obj))
    _prune_handlers()
    return obj


def _mk_event_handler(iid, fn):
    """构造一个「Invoke(sender, args)」形状的**事件**回调（与完成回调形状不同）。"""
    FT = WINFUNCTYPE(ctypes.HRESULT, PVOID, PVOID, PVOID)

    def invoke(this, sender, args):
        try:
            fn(sender, args)
        except Exception:
            pass
        return S_OK

    obj = ComObject(iid, [(FT, invoke)])
    _LIVE_HANDLERS.append((time.time(), obj))
    _prune_handlers()
    return obj


def _prune_handlers():
    """清掉 120 秒前的旧回调对象（回调早该交付完；防每秒轮询把列表撑大）。"""
    if len(_LIVE_HANDLERS) < 64:
        return
    now = time.time()
    _LIVE_HANDLERS[:] = [x for x in _LIVE_HANDLERS if now - x[0] < 120]


# ---------------------------------------------------------------- 运行时检测
def webview2_available():
    """系统里有没有 WebView2 运行时（Win10/11 一般自带）。"""
    if not IS_WIN:
        return False
    import winreg
    KEY = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    paths = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients"),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\EdgeUpdate\Clients"),
    ]
    for hive, base in paths:
        try:
            k = winreg.OpenKey(hive, base + "\\" + KEY)
            pv = winreg.QueryValueEx(k, "pv")[0]
            if pv and pv not in ("0.0.0.0", ""):
                return True
        except OSError:
            continue
    return False

def _find_loader():
    """WebView2Loader.dll 的搜索路径：exe 目录(PyInstaller _MEIPASS) / embed.py 同目录。"""
    cands = []
    try:
        cands.append(os.path.join(sys._MEIPASS, "WebView2Loader.dll"))  # noqa: SLF001
    except Exception:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    cands.append(os.path.join(here, "WebView2Loader.dll"))
    cands.append(os.path.join(os.path.dirname(here), "desktop", "WebView2Loader.dll"))
    for p in cands:
        if os.path.isfile(p):
            return p
    return None

def find_browser():
    """兼容旧接口：可用返回 loader 路径（真值），不可用返回 None。"""
    if not webview2_available():
        return None
    return _find_loader()


def adapter_json_ua(adapter_json):
    """从适配规则 JSON 里读 ua 字段（MOBILE / PC）。"""
    try:
        d = json.loads(adapter_json or "{}")
    except Exception:
        return None
    ua = (d.get("ua") or "").upper()
    if ua == "MOBILE":
        return "MOBILE"
    return None


def ua_string(ua):
    """'MOBILE' / 'PC'(None) → 完整 UA 串。"""
    return MOBILE_UA if ua == "MOBILE" else PC_UA


# ---------------------------------------------------------------- 子窗口铺放
def cover_child_windows(host_hwnd):
    """
    把 host 下的 mpv 子窗口铺满 host 客户区（窗口变大子窗口不会自动跟随）。

    ★ WebView2 控件的子窗口（Chrome_WidgetWin_*）**绝对不能动** —— 它的边界由
    put_Bounds 管，手动 MoveWindow 只会跟官方机制打架。所以这里只跳过类名含
    Chrome_WidgetWin 的子窗口，其余（mpv 的窗口）照旧铺满。
    """
    if not IS_WIN or not host_hwnd:
        return 0
    u32 = ctypes.windll.user32
    rect = wintypes.RECT()
    if not u32.GetClientRect(host_hwnd, byref(rect)):
        return 0
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return 0
    origin = wintypes.POINT(0, 0)
    u32.ClientToScreen(host_hwnd, byref(origin))     # 宿主客户区左上角的屏幕坐标
    n = [0]
    CBTYPE = WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @CBTYPE
    def cb(hwnd, lp):
        cls = ctypes.create_unicode_buffer(256)
        u32.GetClassNameW(hwnd, cls, 256)
        name = cls.value or ""
        if name.startswith("Tk") or "Chrome_WidgetWin" in name:
            return True                      # Tk 自己的 / WebView2 的，都不动
        # ★ v2.10.30：已经在位的就别再 MoveWindow —— 这个方法现在被「常驻守护」
        #   每 500ms 调一次，无谓的 MoveWindow(repaint) 会让正在播的画面抖动。
        cur = wintypes.RECT()
        if u32.GetWindowRect(hwnd, byref(cur)):
            if (cur.left == origin.x and cur.top == origin.y
                    and cur.right - cur.left == w and cur.bottom - cur.top == h):
                return True
        u32.MoveWindow(hwnd, 0, 0, w, h, True)
        n[0] += 1
        return True

    u32.EnumChildWindows(host_hwnd, cb, 0)
    # 注册在本 host 上的 WebView2 也要跟着 fit（put_Bounds 是官方 resize 通道）
    for b in list(_BROWSERS):
        try:
            if b.host_hwnd == host_hwnd:
                b.fit()
        except Exception:
            pass
    return n[0]


# 注册表：host_hwnd -> EmbeddedBrowser（cover_child_windows 用来跟着 fit）
_BROWSERS = []


class EmbeddedBrowser:
    """WebView2 嵌入式浏览器：官方直播页 + wvt_inject.js 全屏化。"""

    def __init__(self, data_dir, log=None, schedule=None):
        self.data_dir = data_dir
        self.log = log or (lambda *a: None)
        # schedule 参数保留仅为兼容旧调用点；新线程模型不再需要 Tk 心跳。
        self._schedule = schedule
        # ★ STA 任务队列：任何线程 _post() 入队（线程安全），专职 STA 线程统一执行。
        self._q = queue.Queue()
        self._wake_tid = 0
        self._quit = False
        self._sta_ready = threading.Event()
        self.ua = None                   # 当前生效的 UA 标记（'MOBILE' / None）
        self.playing_hint = False        # 主页面是否已在播（native_app 维护）
        self._popup_guard_on = False     # 弹窗拦截是否已注册（随 core 重建）
        self.host_hwnd = None
        self.size = (1280, 720)
        self.page_url = ""
        self._hidden = False
        self._closed = False
        self._nav_gen = 0                # 代次：换台/隐藏时作废旧作业
        self._lock = threading.RLock()
        # COM 指针们（全部只准在 STA 线程碰；回调里 AddRef 过，进程期不释放）
        self._env = None
        self._ctrl = None
        self._core = None
        self._settings = None
        self._settings2 = None           # Settings2 指针（能运行时换 UA）
        self._inject_id = None           # 已注册的 document-created 脚本 id
        self._extra_ids = []             # 附加 document-created 脚本（诊断用）
        self._pending_extra = []         # core 未就绪时挂起的附加脚本
        self._owner_thread = threading.current_thread()   # Tk 主线程（建实例者）
        self._udd = os.path.join(data_dir, ".wv2")
        try:
            os.makedirs(self._udd, exist_ok=True)
        except Exception:
            pass
        try:
            atexit.register(self._atexit_close)
        except Exception:
            pass
        # ★ 专职 STA 线程：泵必须先跑起来，之后一切 COM 动作作为任务投进来。
        self._sta = threading.Thread(target=self._sta_main, daemon=True,
                                     name="wv2-sta")
        self._sta.start()
        self._sta_ready.wait(5)

    # ------------------------------------------------------------ 对外接口
    def alive(self):
        return bool(self._ctrl) and not self._closed

    @property
    def hwnd(self):
        """兼容旧检查脚本：播放区里 WebView2 的顶层 Chrome_WidgetWin_0 句柄。"""
        if not (IS_WIN and self.host_hwnd):
            return 0
        found = [0]
        CB = WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def cb(h, lp):
            cls = ctypes.create_unicode_buffer(256)
            ctypes.windll.user32.GetClassNameW(h, cls, 256)
            if (cls.value or "").startswith("Chrome_WidgetWin"):
                found[0] = h
                return False
            return True

        ctypes.windll.user32.EnumChildWindows(self.host_hwnd, CB(cb), 0)
        return found[0]

    @property
    def proc(self):
        """兼容旧检查脚本：WebView2 没有可操作的子进程句柄。"""
        return None

    def play(self, url, adapter_json, extra, inject_src, host_hwnd, w, h):
        """
        打开官方直播页并注入脚本。可在任意线程调用（阻塞到完成，返回 bool）。
        在创建者线程（Tk 主线程）调用会阻塞 UI，但功能正常（边泵消息边等）。
        """
        if not IS_WIN:
            return False
        if self._closed:
            self._closed = False
        self._nav_gen += 1
        gen = self._nav_gen
        self.host_hwnd = host_hwnd
        self.size = (max(2, int(w)), max(2, int(h)))
        if self not in _BROWSERS:
            _BROWSERS.append(self)
        box, ev = {}, threading.Event()
        job = self._job_play(url, adapter_json, extra, inject_src, gen)
        self._start_job(job, ev, box, gen, "play")
        if not self._wait_ev(ev, 75):
            self.log("play 超时（75s）—— STA 泵没在转？")
            return False
        if isinstance(box.get("err"), Exception):
            raise box["err"]
        return bool(box.get("ret"))

    def warmup(self, host_hwnd, w, h):
        """预热：提前把 WebView2 环境/控件建好，停在 about:blank，并先隐藏。

        首次创建要加载 WebView2Loader.dll、拉起 Edge 进程、建环境 —— 实测
        1~2 秒，全落在用户点频道之后。启动空转时先付掉这笔钱，点频道时
        直接导航官方页，省下这段等待。任意线程可调（阻塞到完成，返回 bool）。
        """
        if not IS_WIN or self._closed:
            return False
        self.host_hwnd = host_hwnd
        self.size = (max(2, int(w)), max(2, int(h)))
        self._nav_gen += 1
        gen = self._nav_gen
        box, ev = {}, threading.Event()
        job = self._job_warmup(gen)
        self._start_job(job, ev, box, gen, "warmup")
        if not self._wait_ev(ev, 30):
            self.log("warmup 超时（30s）")
            return False
        return bool(box.get("ret"))

    def state(self):
        """注入脚本自报的状态：'' / playing / failed / closed。任意线程可调。"""
        return self._eval_sync("(window.__wvt_state||'')", timeout=6) or ""

    def page_reason(self):
        """注入脚本报失败时附带的原因（站点提示文本）。任意线程可调。"""
        return self._eval_sync("(window.__wvt_reason||'')", timeout=5) or ""

    def page_ready(self):
        return self._eval_sync("document.readyState", timeout=5) == "complete"

    def show(self):
        """恢复可见（Tk 回调直接调，非阻塞）。"""
        self._hidden = False

        def task():
            self._fit_now()
            if not self._hidden:
                self._put_visible(True)

        self._post(task)

    def hide(self):
        """
        暂时隐藏（切回本机源）：控件藏起来 + 异步把页面停掉/清空（不然声音还在响）。
        进程/控件保留，秒切回来。Tk 回调直接调，非阻塞。
        """
        self._hidden = True
        gen = self._nav_gen

        def task():
            try:
                self._put_visible(False)
            except Exception:
                pass
            self._start_job(self._job_quiet_page(gen), None, None, gen, "hide")

        self._post(task)

    def notify_parent_moved(self):
        """主窗口移动后调一次：WebView2 需要同步它的弹层位置。"""
        def task():
            if self._ctrl:
                try:
                    _vtbl_call(self._ctrl, 23, _FN_CTRL_VOID)
                except Exception:
                    pass

        self._post(task)

    def resize(self, w, h):
        self.size = (max(2, int(w)), max(2, int(h)))
        self.fit()

    def fit(self):
        """把 WebView2 铺满 host 客户区（put_Bounds 是官方唯一 resize 通道）。"""
        self._post(self._fit_now)

    # ------------------------------------------------------------ STA 内部动作
    def _fit_now(self):
        """只准在 STA 线程调。"""
        if not (self._ctrl and self.host_hwnd and IS_WIN):
            return
        try:
            u32 = ctypes.windll.user32
            r = wintypes.RECT()
            if not u32.GetClientRect(self.host_hwnd, byref(r)):
                return
            w, h = r.right - r.left, r.bottom - r.top
            if w < 2 or h < 2:
                return
            self.size = (w, h)
            _vtbl_call(self._ctrl, 6, _FN_PUT_BOUNDS, wintypes.RECT(0, 0, w, h))
            if not self._hidden:
                self._put_visible(True)
        except Exception as e:               # noqa: BLE001
            self.log("fit 失败：%r" % (e,))

    def _put_visible(self, on):
        if self._ctrl:
            _vtbl_call(self._ctrl, 4, _FN_PUT_INT, 1 if on else 0)

    def _get_ptr(self, obj, index):
        """get_* [out] 接口指针的标准姿势：byref(c_void_p()) 取 .value。"""
        out = c_void_p()
        hr = _vtbl_call(obj, index, _FN_GET_PTR, byref(out))
        if hr == S_OK and out.value:
            return out.value
        return None

    def _addref(self, ptr):
        try:
            _vtbl_call(ptr, 1, FN_REFCOUNT)
        except Exception:
            pass

    def close(self):
        """彻底关闭。任意线程可调；创建者线程上会边泵消息边等（最多 8s）。"""
        with self._lock:
            self._nav_gen += 1
            self._closed = True
            if self not in _BROWSERS:
                return
            box, ev = {}, threading.Event()
            self._start_job(self._job_close(), ev, box, self._nav_gen, "close")
            self._wait_ev(ev, 8)
            try:
                _BROWSERS.remove(self)
            except ValueError:
                pass

    def _atexit_close(self):
        try:
            if self._env or self._ctrl:
                self.close()
        except Exception:
            pass

    # ------------------------------------------------------------ STA 线程与泵
    def _sta_main(self):
        """专职 STA 线程：CoInitializeEx + 消息泵 + 任务排水（全项目心脏）。"""
        try:
            u32 = ctypes.windll.user32
            k32 = ctypes.windll.kernel32
            ole32 = ctypes.windll.ole32
            # 显式签名：默认转换下 64 位 WPARAM/LPARAM 会溢出
            u32.MsgWaitForMultipleObjects.argtypes = [
                wintypes.DWORD, POINTER(wintypes.HANDLE), wintypes.BOOL,
                wintypes.DWORD, wintypes.DWORD]
            u32.MsgWaitForMultipleObjects.restype = wintypes.DWORD
            u32.PeekMessageW.argtypes = [POINTER(wintypes.MSG), wintypes.HWND,
                                         wintypes.UINT, wintypes.UINT,
                                         wintypes.DWORD]
            u32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                               wintypes.WPARAM, wintypes.LPARAM]

            hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
            if hr not in (S_OK, S_FALSE, RPC_E_CHANGED_MODE):
                self.log("WebView2 STA CoInitializeEx 失败 hr=0x%08X"
                         % (hr & 0xFFFFFFFF))
                self._sta_ready.set()
                return
            self._wake_tid = k32.GetCurrentThreadId()
            self._sta_ready.set()

            msg = wintypes.MSG()
            while not self._quit:
                # 最多睡 200ms；有新任务会 PostThreadMessage 立刻叫醒
                u32.MsgWaitForMultipleObjects(0, None, False, 200, QS_ALLINPUT)
                while u32.PeekMessageW(byref(msg), None, 0, 0, 1):
                    u32.TranslateMessage(byref(msg))
                    u32.DispatchMessageW(byref(msg))
                self._drain()
            ole32.CoUninitialize()
        except Exception as e:               # noqa: BLE001
            self._sta_ready.set()
            self.log("WebView2 STA 线程异常退出：%r" % (e,))

    def _drain(self):
        """排水：跑掉到期任务。★ 到期判断用「当时」的时间，绝不自旋。"""
        batch, rest = [], []
        while True:
            try:
                when, fn = self._q.get_nowait()
            except queue.Empty:
                break
            if when <= time.time():
                batch.append(fn)
            else:
                rest.append((when, fn))
        for item in rest:
            self._q.put(item)
        for fn in batch:
            try:
                fn()
            except Exception as e:           # noqa: BLE001
                self.log("WebView2 任务异常：%r" % (e,))

    def _post(self, fn, delay_ms=0):
        """线程安全投递任务到 STA 泵（最多 200ms 延迟，_wake 可立刻叫醒）。"""
        self._q.put((time.time() + max(0, int(delay_ms)) / 1000.0, fn))
        self._wake()
        return True

    def _wake(self):
        tid = self._wake_tid
        if tid:
            try:
                ctypes.windll.user32.PostThreadMessageW(tid, WM_NULL, 0, 0)
            except Exception:
                pass

    def _wait_ev(self, ev, timeout):
        """
        等事件。★ 创建者线程（Tk 主线程）上必须边泵消息边等 —— WebView2 会向
        parent 窗口所属线程发同步消息，主线程干等不泵就是死锁（实测）。
        """
        if IS_WIN and threading.current_thread() is self._owner_thread:
            t0 = time.time()
            u32 = ctypes.windll.user32
            m = wintypes.MSG()
            while not ev.wait(0.03):
                while u32.PeekMessageW(byref(m), None, 0, 0, 1):
                    u32.TranslateMessage(byref(m))
                    u32.DispatchMessageW(byref(m))
                if time.time() - t0 > timeout:
                    return False
            return True
        return ev.wait(timeout)

    # ------------------------------------------------------------ 作业驱动
    def _start_job(self, genfn, done_ev, box, gen, tag):
        """在 STA 泵上驱动一个生成器作业；gen 变了就立刻作废。"""

        def step():
            if self._quit:
                _finish(False, err=None)
                return
            if self._closed and tag != "close":
                _finish(False, err=None)
                return
            if gen != self._nav_gen and tag in ("play", "eval"):
                _finish(False, err=None)
                return
            try:
                d = next(genfn)
                self._post(step, d or 0)
            except StopIteration as e:
                _finish(e.value, err=None)
            except Exception as e:           # noqa: BLE001
                _finish(None, err=e)

        def _finish(ret, err):
            if box is not None:
                if err is not None:
                    box["err"] = err
                else:
                    box["ret"] = ret
            if done_ev is not None:
                done_ev.set()

        self._post(step, 0)

    def _eval_sync(self, expr, timeout=6):
        """任意线程 evaluate JS 并等结果。失败原因存 self.last_eval_err。"""
        if not self._core or self._closed:
            self.last_eval_err = "no-core/closed"
            return None
        box, ev = {}, threading.Event()
        self._start_job(self._job_eval(expr), ev, box, self._nav_gen, "eval")
        if not self._wait_ev(ev, timeout):
            self.last_eval_err = "wait-timeout(%ss)" % timeout
            return None
        if "err" in box:
            self.last_eval_err = "job-err: %r" % (box["err"],)
            return None
        if "ret" not in box:
            self.last_eval_err = "job-cancelled(nav_gen)"
            return None
        self.last_eval_err = None
        return box.get("ret")

    # ------------------------------------------------------------ 各类作业
    def _job_play(self, url, adapter_json, extra, inject_src, gen):
        """生成器：确保控件 → 设 UA/注入 → 导航 → 补注入 → 铺满。"""
        ua = adapter_json_ua(adapter_json)
        ok = yield from self._job_ensure(ua, gen)
        if not ok or gen != self._nav_gen:
            return False
        try:
            # ① 换台注入：先撤旧脚本再注册新的（adapter/extra 每个频道都不同）
            prelude = ("window.wvt_adapter = %s;\n"
                       "window.wvt_extra = %s;\n"
                       "window.NativeTV = {"
                       " pagePlaying: function(){window.__wvt_state='playing';},"
                       " pageFailed:  function(r){window.__wvt_state='failed';"
                       "   window.__wvt_reason=(r==null?'':(''+r));},"
                       " closePage:   function(){window.__wvt_state='closed';}"
                       "};\n") % (adapter_json or "{}", json.dumps(extra or ""))
            full = ("(function(){function boot(){\n%s\n%s\n}"
                    "if(document.readyState==='loading')"
                    "{document.addEventListener('DOMContentLoaded',boot);}"
                    "else{boot();}})()") % (prelude, inject_src)
            self._register_inject(full)
            # ② 导航
            _vtbl_call(self._core, 5, _FN_NAVIGATE, c_wchar_p(url))
            self.page_url = url
            yield 1500
            if gen != self._nav_gen:
                return False
            # ③ 首屏兜底：直接补一次（wvt_inject.js 幂等，__wvtInjected 守卫）
            self._eval_fire("window.__wvt_state='';window.__wvt_reason='';")
            self._eval_fire(full)
            yield 600
            if gen != self._nav_gen:
                return False
            self._eval_fire(full)
            # ④ 铺满 + 可见
            self._fit_now()
            self._hidden = False
            # ★ 已知 bug（WebView2Feedback#1077）：宿主窗口在后台时创建的控件
            #   不出画面（Chromium 窗口树保持隐藏）。官方 workaround：导航完成
            #   后把可见性 false→true 翻一次，触发 OnVisibleChanged 重新应用。
            try:
                _vtbl_call(self._ctrl, 4, _FN_PUT_INT, 0)
                yield 40
                _vtbl_call(self._ctrl, 4, _FN_PUT_INT, 1)
            except Exception:
                pass
            self.log("WebView2 已打开官方页面：%s" % url[:100])
            return True
        except Exception as e:               # noqa: BLE001
            self.log("导航/注入失败：%r" % (e,))
            return False

    def _job_warmup(self, gen):
        """生成器：只建环境/控件 + 停在空白页，不碰官方页、不注册注入脚本。"""
        ok = yield from self._job_ensure(None, gen)
        if not ok or gen != self._nav_gen:
            return False
        try:
            self._fit_now()
            if self._core:
                _vtbl_call(self._core, 5, _FN_NAVIGATE,
                           c_wchar_p("about:blank"))
                self.page_url = ""
            # 立刻藏起来 —— 此刻用户还没点频道，播放区该显示占位提示，
            # 不能让一块空白 WebView2 盖在上面。_job_play 里会再翻回可见。
            self._hidden = True
            self._put_visible(False)
            self.log("WebView2 预热完成（空白页待命）")
        except Exception as e:               # noqa: BLE001
            self.log("预热收尾失败：%r" % (e,))
        return True

    def _job_ensure(self, ua, gen):
        """生成器：保证 env/controller/core 就绪，UA 不同就运行时切换。"""
        if self._ctrl and self._core:
            if (ua or None) == (self.ua or None):
                return True
            # Settings2 运行时换 UA —— 连控件都不用重建
            if self._settings2:
                try:
                    _vtbl_call(self._settings2, 22, _FN_PUT_WSTR,
                               c_wchar_p(ua_string(ua)))
                    self.ua = ua
                    return True
                except Exception:
                    pass
            self._close_now()                # 运行时换不了 → 重建
            yield 100
        # —— 创建 environment（★ 任务已在泵里跑 = 官方要求的「泵着的线程」）——
        loader = _find_loader()
        if not loader:
            self.log("找不到 WebView2Loader.dll（打包遗漏）")
            return False
        if not webview2_available():
            self.log("系统没有 WebView2 运行时")
            return False
        try:
            dll = ctypes.WinDLL(loader, use_last_error=True)
            create = dll.CreateCoreWebView2EnvironmentWithOptions
            create.argtypes = [c_wchar_p, c_wchar_p, PVOID, PVOID]
            create.restype = ctypes.HRESULT
        except Exception as e:               # noqa: BLE001
            self.log("加载 WebView2Loader.dll 失败：%r" % (e,))
            return False

        # ★ 不用 ICoreWebView2EnvironmentOptions 对象，改走官方环境变量通道：
        #   WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS / WEBVIEW2_USER_DATA_FOLDER。
        #   原因（实测对照结论）：自己实现 options COM 对象时，QI 只认到
        #   Options7 → loader 报 0x80070002；认到 Options8 → 直接访问违例。
        #   环境变量是官方文档明载的配置通道，loader 同步读取 —— 实测一次通过。
        #   ★ 与外部预置值合并（追加而不是覆盖）：调用方可以在创建前塞自己的
        #     参数（诊断实验 / 用户自定义），否则会被这里覆盖丢弃。
        _prev = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "")
        os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = \
            (_prev + " " + EXTRA_ARGS).strip()
        os.environ["WEBVIEW2_USER_DATA_FOLDER"] = os.path.abspath(self._udd)

        ev = threading.Event()
        box = {}

        def on_env(err, env):
            box["err"] = err
            if err == S_OK and env:
                self._addref(env)        # 回调只做 AddRef + 存指针（铁律 3）
                box["env"] = env
            ev.set()

        handler = _mk_handler(IID_ENV_HANDLER, on_env)
        self._env = None
        hr = create(None, None, None, handler.ptr())
        # create() 返回时浏览器进程已带着环境变量副本启动，恢复原值
        # 免得漏给本进程之后起的其它子进程（嗅探浏览器等）。
        try:
            if _prev:
                os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = _prev
            else:
                os.environ.pop("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", None)
            os.environ.pop("WEBVIEW2_USER_DATA_FOLDER", None)
        except Exception:
            pass
        if hr != S_OK and hr != S_FALSE:
            self.log("创建 WebView2 环境失败 hr=0x%08X（profile 被占？）"
                     % (hr & 0xFFFFFFFF))
            return False
        # ★ 绝不能在这里 ev.wait() —— 会把 STA 线程堵死，泵派发不了回调（死锁）。
        #   必须 yield 让泵转起来，回调经消息循环交付后再检查（实测铁律）。
        t0 = time.time()
        while not ev.is_set():
            if time.time() - t0 > 30 or gen != self._nav_gen:
                self.log("WebView2 环境创建超时/被取代")
                return False
            yield 80
        if box["err"] != S_OK or not box.get("env"):
            self.log("WebView2 环境创建失败 hr=0x%08X"
                     % ((box["err"] or 0) & 0xFFFFFFFF))
            return False
        self._env = box["env"]

        # —— 创建 controller（★ parent 直接挂 Tk 主线程的 host hwnd：
        #      跨线程 parent 可行，前提是 parent 所属线程在泵消息 ——
        #      Tk mainloop 天然满足）——
        ev2 = threading.Event()
        box2 = {}

        def on_ctrl(err, ctrl):
            box2["err"] = err
            if err == S_OK and ctrl:
                self._addref(ctrl)
                box2["ctrl"] = ctrl
            ev2.set()

        handler2 = _mk_handler(IID_CTRL_HANDLER, on_ctrl)
        self._ctrl = None
        _vtbl_call(self._env, 3, _FN_ENV_CREATE_CTRL,
                   wintypes.HWND(self.host_hwnd or None), handler2.ptr())
        t0 = time.time()
        while not ev2.is_set():
            if time.time() - t0 > 30 or gen != self._nav_gen:
                self.log("WebView2 控件创建超时/被取代")
                return False
            yield 80
        if box2["err"] != S_OK or not box2.get("ctrl"):
            self.log("WebView2 控件创建失败 hr=0x%08X"
                     % ((box2["err"] or 0) & 0xFFFFFFFF))
            return False
        self._ctrl = box2["ctrl"]
        # —— core / settings（★ 二级指针出参：byref(c_void_p()) 取 .value）——
        self._core = self._get_ptr(self._ctrl, 25)
        if not self._core:
            self.log("get_CoreWebView2 失败")
            return False
        self._settings = self._get_ptr(self._core, 3)
        self._settings2 = None
        if self._settings:
            s2 = c_void_p()
            hr = _vtbl_call(self._settings, 0, _FN_QI,
                            byref(IID_SETTINGS2), byref(s2))
            if hr == S_OK and s2.value:
                self._settings2 = s2.value
        self.ua = ua
        if not self._settings2:
            self.log("Settings2 不可用，UA 切换将重建环境")
        # 关掉默认右键菜单（观看界面不需要，还容易误触）
        try:
            _vtbl_call(self._settings, 14, _FN_PUT_INT, 0)   # AreDefaultContextMenusEnabled
        except Exception:
            pass
        self._register_popup_guard()
        self.log("WebView2 就绪（UA=%s 原生切换=%s）"
                 % (ua or "PC", bool(self._settings2)))
        self._flush_pending_extra()
        return True

    def _register_popup_guard(self):
        """注册 NewWindowRequested 拦截（每次 core 重建后调一次）。

        ★ 不拦截的后果（实测）：页面 window.open / target=_blank 弹出的
          WebView2 独立顶层窗口归本 exe 所有 —— 任务栏显示成主程序图标、
          里面照播视频、关主窗口也不跟着关；切一次台攒一个，用户看到的
          就是「桌面多了好几个纯净直播窗口」。
        处置：弹窗一律拦下（put_Handled）；若主页面还没播起来，弹窗很
        可能才是真正的播放页 → 主控件跟进导航过去。
        """
        if not self._core:
            return
        self._popup_guard_on = True

        def on_new_window(sender, args):
            uri = None
            try:
                out = c_void_p()                       # get_Uri（vtbl 3）
                if _vtbl_call(args, 3, _FN_GET_WSTR, byref(out)) == S_OK \
                        and out.value:
                    uri = cast(out, c_wchar_p).value
                    ctypes.windll.ole32.CoTaskMemFree(out)
            except Exception:
                pass
            try:
                _vtbl_call(args, 6, _FN_PUT_INT, 1)    # put_Handled = true
            except Exception:
                pass
            if uri:
                u = uri
                http = u.startswith("http")
                self.log("已拦截弹窗：%s%s" % (u[:90], "" if self.playing_hint
                                              else "（页面未播 → 跟进导航）"))
                # ★ 只有真实网页才跟进；window.open('') 之类的空白弹窗
                #   （about:blank）跟过去会把还没起播的主页面整个冲掉
                if not self.playing_hint and http:
                    self._post(lambda: self._follow_popup(u), 0)

        h = _mk_event_handler(IID_NEWWIN_HANDLER, on_new_window)
        try:
            tok = c_longlong(0)
            _vtbl_call(self._core, 44, _FN_ADD_NEWWIN, h.ptr(), byref(tok))
        except Exception as e:                   # noqa: BLE001
            self.log("注册弹窗拦截失败：%r" % (e,))

    def _follow_popup(self, url):
        """主控件跟进弹窗 URL（只准在 STA 泵上跑）。"""
        if self._closed or not self._core:
            return
        try:
            _vtbl_call(self._core, 5, _FN_NAVIGATE, c_wchar_p(url))
            self.page_url = url
        except Exception as e:                   # noqa: BLE001
            self.log("弹窗跟进导航失败：%r" % (e,))

    def _job_quiet_page(self, gen):
        """生成器：把页面静音/停流并导航到 about:blank（代次守卫）。"""
        try:
            self.log("quiet_page：开始（gen=%d page_url=%s）" % (gen, self.page_url))
            if gen != self._nav_gen or not self._core:
                self.log("quiet_page：代次/核心不符，放弃")
                return
            self._eval_fire(
                "(function(){var v=document.getElementsByTagName('video');"
                "for(var i=0;i<v.length;i++){try{v[i].pause();v[i].muted=true;"
                "v[i].removeAttribute('src');v[i].load();}catch(e){}}"
                "window.__wvt_state='';window.__wvt_reason='';return 1;})()")
            yield 120
            if gen != self._nav_gen or not self._core:
                return
            _vtbl_call(self._core, 5, _FN_NAVIGATE, c_wchar_p("about:blank"))
            self.page_url = ""
        except Exception:
            pass

    def _job_eval(self, expr):
        """生成器：执行 JS 并把结果交回。"""
        if not self._core:
            return None
        slot = {}

        def on_done(err, res):
            slot["err"] = err
            slot["v"] = res if err == S_OK else None

        h = _mk_handler(IID_EXEC_HANDLER, on_done)
        _vtbl_call(self._core, 29, _FN_EXEC, c_wchar_p(expr), h.ptr())
        t0 = time.time()
        while "v" not in slot:
            if time.time() - t0 > 8:
                self.last_eval_err = "exec-no-callback(8s)"
                return None
            yield 60
        if slot.get("err") not in (None, S_OK):
            self.last_eval_err = "exec-hr=0x%08X" % (slot["err"] & 0xFFFFFFFF)
        raw = slot["v"]
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return raw

    def _job_close(self):
        try:
            self._close_now()
        except Exception:
            pass
        yield 0
        return True

    def _close_now(self):
        try:
            if self._ctrl:
                _vtbl_call(self._ctrl, 24, _FN_CTRL_VOID)      # Close()
        except Exception:
            pass
        self._ctrl = None
        self._core = None
        self._settings = None
        self._settings2 = None
        self._env = None
        self._inject_id = None
        self._extra_ids = []
        self._popup_guard_on = False
        self.playing_hint = False
        self.page_url = ""
        self._hidden = False

    # ------------------------------------------------------------ 小工具
    def _register_inject(self, full):
        """注册「每个新文档都执行」的脚本（先撤旧的 —— 换台要换 adapter）。"""
        if not self._core:
            return
        try:
            if self._inject_id:
                _vtbl_call(self._core, 28, _FN_PUT_WSTR,
                           c_wchar_p(self._inject_id))
        except Exception:
            pass
        self._inject_id = None

        def on_id(err, sid):
            self._inject_id = sid

        h = _mk_handler(IID_ADDSCRIPT_HANDLER, on_id)
        _vtbl_call(self._core, 27, _FN_EXEC, c_wchar_p(full), h.ptr())

    def register_extra(self, js):
        """注册一段「每个新文档都执行」的附加脚本（独立 id，不随换台覆盖）。
        线程安全。core 未就绪时先挂起，_job_ensure 建好 core 后自动补注册。
        诊断探针用，不影响 _job_play 的主注入。"""
        def task():
            if self._core:
                self._register_extra_now(js)
            else:
                self._pending_extra.append(js)
        self._post(task, 0)

    def _register_extra_now(self, js):
        if not self._core:
            return
        h = _mk_handler(IID_ADDSCRIPT_HANDLER,
                        lambda err, sid: self._extra_ids.append(sid))
        try:
            _vtbl_call(self._core, 27, _FN_EXEC, c_wchar_p(js), h.ptr())
        except Exception:
            pass

    def _flush_pending_extra(self):
        """只准在 STA 线程调（_job_ensure 建好 core 后）。"""
        pend, self._pending_extra = self._pending_extra, []
        for js in pend:
            self._register_extra_now(js)

    def _eval_fire(self, expr):
        """发一条 JS 不等结果。线程安全：非 STA 线程调用时自动投递过去
        （COM 调用必须留在创建线程，跨线程直调报 0x802A000C）。"""
        if not self._core or self._closed:
            return
        if threading.current_thread() is not self._owner_thread:
            self._post(lambda: self._eval_fire(expr), 0)
            return
        try:
            h = _mk_handler(IID_EXEC_HANDLER, lambda err, res: None)
            _vtbl_call(self._core, 29, _FN_EXEC, c_wchar_p(expr), h.ptr())
        except Exception:
            pass


# ---------------------------------------------------------------- vtable 签名
_FN_QI = FN_QI
_FN_GET_WSTR = WINFUNCTYPE(ctypes.HRESULT, PVOID, POINTER(c_void_p))
_FN_PUT_WSTR = WINFUNCTYPE(ctypes.HRESULT, PVOID, c_wchar_p)
_FN_GET_INT = WINFUNCTYPE(ctypes.HRESULT, PVOID, POINTER(ctypes.c_int))
_FN_PUT_INT = WINFUNCTYPE(ctypes.HRESULT, PVOID, ctypes.c_int)
_FN_ENV_CREATE_CTRL = WINFUNCTYPE(ctypes.HRESULT, PVOID, wintypes.HWND, PVOID)
_FN_GET_PTR = WINFUNCTYPE(ctypes.HRESULT, PVOID, POINTER(PVOID))
_FN_NAVIGATE = WINFUNCTYPE(ctypes.HRESULT, PVOID, c_wchar_p)
_FN_EXEC = WINFUNCTYPE(ctypes.HRESULT, PVOID, c_wchar_p, PVOID)
_FN_PUT_BOUNDS = WINFUNCTYPE(ctypes.HRESULT, PVOID, wintypes.RECT)
_FN_CTRL_VOID = WINFUNCTYPE(ctypes.HRESULT, PVOID)
_FN_BAD2 = WINFUNCTYPE(ctypes.HRESULT, PVOID, PVOID, PVOID)
# add_NewWindowRequested(This, handler, EventRegistrationToken*) —— token 是
# 一个 int64 值结构，这里只当出口槽用（不打算反注册，进程期不 remove）
_FN_ADD_NEWWIN = WINFUNCTYPE(ctypes.HRESULT, PVOID, PVOID,
                             POINTER(c_longlong))

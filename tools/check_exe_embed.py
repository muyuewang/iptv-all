# -*- coding: utf-8 -*-
"""
exe 端到端：官网源播放时，浏览器到底有没有「跑出来变成一个独立应用」。

用户原话：
    「exe版本播放视频怎么又打开一个应用 没法像本地源一样在自己这个页面内观看」

这个脚本跑的是**真正的 desktop/dist/电视直播.exe**（不是 native_app.py 源码），
用模拟鼠标点开「官网源」再点一个频道，然后全程盯着：

    有没有任何一个 Chrome_WidgetWin* 顶层窗口
    「可见」且「压在屏幕上」且「不是 exe 主窗口的后代窗口」？

有 → 用户就会看到浏览器单独蹦出来，判失败。
顺带把每次露脸的时刻 / 矩形都打出来，便于定位。

用法：python tools/check_exe_embed.py
"""
import os
import re
import sys
import time
import ctypes
import subprocess
import threading
from ctypes import wintypes

from PIL import ImageGrab

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
EXE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "desktop", "dist", "电视直播.exe")
SHOT_DIR = os.path.join(ROOT, "_shots")
os.makedirs(SHOT_DIR, exist_ok=True)

# ★ 必须先把本进程标成 DPI 感知，否则坐标系会打架：
#   本机显示缩放是 125%，DPI-unaware 进程拿到的 ClientToScreen/GetWindowRect 是
#   **虚拟化**坐标，而 ImageGrab.grab 吃的是**物理**像素 —— 结果截图裁偏、
#   按截图算出来的点击坐标也全偏（实测点到隔壁那个已经选中的胶囊上，看起来「点了没反应」）。
ctypes.windll.user32.SetProcessDPIAware()

u = ctypes.windll.user32
_CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
u.EnumWindows.argtypes = [_CB, wintypes.LPARAM]
u.EnumChildWindows.argtypes = [wintypes.HWND, _CB, wintypes.LPARAM]
u.GetWindowTextW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
u.GetClassNameW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
u.GetWindowRect.restype = wintypes.BOOL
u.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
u.GetClientRect.restype = wintypes.BOOL
u.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
u.ClientToScreen.restype = wintypes.BOOL
u.IsWindowVisible.argtypes = [wintypes.HWND]
u.IsWindowVisible.restype = wintypes.BOOL
u.IsChild.argtypes = [wintypes.HWND, wintypes.HWND]
u.IsChild.restype = wintypes.BOOL
u.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
u.GetWindow.restype = wintypes.HWND

VSCREEN = (u.GetSystemMetrics(76), u.GetSystemMetrics(77),
           u.GetSystemMetrics(78), u.GetSystemMetrics(79))

FAIL = []
STRAY = []
T0 = time.time()


def check(cond, msg):
    print(("  [OK] " if cond else "  [!!] ") + msg)
    sys.stdout.flush()
    if not cond:
        FAIL.append(msg)


def info(msg):
    print("  " + msg)
    sys.stdout.flush()


def client_rect_on_screen(hwnd):
    cr = wintypes.RECT()
    pt = wintypes.POINT(0, 0)
    if not u.GetClientRect(hwnd, ctypes.byref(cr)):
        return None
    if not u.ClientToScreen(hwnd, ctypes.byref(pt)):
        return None
    return (pt.x, pt.y, pt.x + cr.right - cr.left, pt.y + cr.bottom - cr.top)


def _rect(hwnd):
    r = wintypes.RECT()
    if not u.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return (r.left, r.top, r.right, r.bottom)


def overlap_area(rect):
    if not rect:
        return 0
    l, t, r, b = rect
    w = max(0, min(r, VSCREEN[0] + VSCREEN[2]) - max(l, VSCREEN[0]))
    h = max(0, min(b, VSCREEN[1] + VSCREEN[3]) - max(t, VSCREEN[1]))
    return w * h


def find_exe_window(timeout=40):
    """找 exe 的主窗口：PyInstaller onefile 会 fork 子进程，按 pid 找不到，只能认类名+标题。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        hits = []

        @_CB
        def cb(hwnd, lp):
            cls = ctypes.create_unicode_buffer(128)
            u.GetClassNameW(hwnd, cls, 128)
            if not cls.value.startswith("Tk"):
                return True
            tt = ctypes.create_unicode_buffer(256)
            u.GetWindowTextW(hwnd, tt, 256)
            if "纯净电视" in tt.value or "电视直播" in tt.value:
                hits.append(hwnd)
            return True

        u.EnumWindows(cb, 0)
        if hits:
            return hits[0]
        time.sleep(0.4)
    return None


def all_children(root):
    """root 的全部后代窗口（含自身），用来判断浏览器是不是已经嵌进来了。"""
    out = set([root])
    stack = [root]

    @_CB
    def cb(hwnd, lp):
        out.add(hwnd)
        return True

    while stack:
        h = stack.pop()
        u.EnumChildWindows(h, cb, 0)
    return out


def chrome_windows(descendants):
    """本机所有 Chrome_WidgetWin* 顶层窗口 → [(hwnd, rect, visible, is_inside)]。"""
    res = []

    @_CB
    def cb(hwnd, lp):
        cls = ctypes.create_unicode_buffer(128)
        u.GetClassNameW(hwnd, cls, 128)
        if not cls.value.startswith("Chrome_WidgetWin"):
            return True
        r = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))
        res.append((hwnd, (r.left, r.top, r.right, r.bottom),
                    bool(u.IsWindowVisible(hwnd)), hwnd in descendants))
        return True

    u.EnumWindows(cb, 0)
    return res


STOP = threading.Event()
STATE = {"root": None, "inside_seen": False}


def monitor():
    """
    全程监视：任何「可见 + 压在屏幕上 + 不在 exe 窗口里」的 Chromium 顶层窗口都记账。

    ★ 只按「可见 + 在屏幕内」判断，不看 pid：真实浏览器进程跟 exe 不是一个进程，
      而且这台机器上 WorkBuddy / QQ / Clash 全是 Electron，也带 Chrome_WidgetWin_1 ——
      但它们**不是我们刚启动的**。用「exe 窗口的后代集合」来区分：
      我们自己嵌进去的那个一定是后代；其它的一律不管，除非它在监控窗口期内**新出现**。

    ★ 顺带说明为什么这里不需要「嵌进来了没有」这个状态：
      本函数用 `EnumWindows` 枚举，而 **EnumWindows 只枚举顶层窗口**。
      一旦 SetParent 成功，浏览器窗口就变成子窗口，会从顶层列表里**整个消失** ——
      所以它压根不会进入下面这个循环。反过来，只有当它被 kiosk 之类从宿主里
      **摘出去变回顶层窗口**时，EnumWindows 才又能看见它，那时它才应该被记为 STRAY。
      也就是说：「能被 EnumWindows 看见」本身就是「它跑出来了」的同义词。
    """
    known_before = set(h for h, _r, _v, _i in chrome_windows(set()))
    while not STOP.is_set():
        root = STATE["root"]
        if root:
            desc = all_children(root)
            for hwnd, rect, vis, inside in chrome_windows(desc):
                if inside:
                    STATE["inside_seen"] = True
                    continue
                if not vis:
                    continue
                if overlap_area(rect) < 100 * 100:
                    continue
                if hwnd in known_before:
                    continue        # 启动前就存在的（用户的 Edge / 其它 Electron 应用）
                STRAY.append((round(time.time() - T0, 2), "%x" % hwnd, rect))
        time.sleep(0.05)


# ---------------------------------------------------------------- 模拟鼠标
# ★ 为什么两种都留着：在「由 agent 拉起的后台进程」里，注入式点击不一定被放行。
#   mouse_event 最接近真实点击（双击也能被 Tk 正常识别），PostMessage 兜底。
#   实测：坐标算对（DPI 感知）之后 mouse_event 是有效的 —— 之前点不动是因为
#   算出来的坐标偏了 25%，点到了隔壁那个已经选中的胶囊上。
u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
u.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                          wintypes.DWORD, ctypes.c_void_p]
u.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
u.PostMessageW.restype = wintypes.BOOL
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
MK_LBUTTON = 0x0001
CLICK_METHOD = ["mouse"]          # 记住哪种方式有效，后面统一用


def click_mouse(sx, sy, double=False):
    """物理屏幕坐标点击。"""
    u.SetCursorPos(int(sx), int(sy))
    time.sleep(0.1)
    n = 2 if double else 1
    for i in range(n):
        u.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, None)
        time.sleep(0.03)
        u.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, None)
        if i == 0 and double:
            time.sleep(0.06)
    time.sleep(0.3)


def click_post(hwnd, cx, cy, double=False):
    """把鼠标消息直接投给窗口（客户区坐标）。"""
    u.SetCursorPos(int(cx), int(cy))
    lp = (int(cy) << 16) | (int(cx) & 0xFFFF)
    u.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lp)
    n = 2 if double else 1
    for i in range(n):
        u.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lp)
        time.sleep(0.02)
        u.PostMessageW(hwnd, WM_LBUTTONUP, 0, lp)
        if i == 0 and double:
            time.sleep(0.06)
    time.sleep(0.3)


def click(hwnd, app_rect, cx, cy, double=False):
    """按当前有效的注入方式，在客户区坐标 (cx, cy) 处点击。"""
    m = CLICK_METHOD[0]
    if m == "mouse":
        click_mouse(app_rect[0] + cx, app_rect[1] + cy, double)
    else:
        click_post(hwnd, cx, cy, double)


def find_selected_pill(img, top_h=62, x_max=760):
    """
    在顶栏里找**选中的那个胶囊**（品牌红橙渐变，BRAND2 #ff6b35 → BRAND #ff2d55）。

    ★ 不能直接对匹配到的像素取全局 min/max —— 标题前面那个 "● " 圆点颜色一模一样，
      两者会被合并成一个横跨整条顶栏的假 bbox（实测合出来 302×19，点下去点歪）。
      要按 x 方向的间隙做聚类，取**最左边那个宽度 ≥40 的簇**（= 选中的胶囊）。
    """
    px = img.load()
    w, h = img.size
    xs = set()
    ymin, ymax = 10 ** 9, -1
    for y in range(0, min(top_h, h)):
        for x in range(0, min(x_max, w)):
            r, g, b = px[x, y][:3]
            if r > 200 and g < 170 and b < 170 and (r - b) > 60:
                xs.add(x)
                ymin = min(ymin, y)
                ymax = max(ymax, y)
    if not xs:
        return None
    ordered = sorted(xs)
    groups, cur = [], [ordered[0]]
    for x in ordered[1:]:
        if x - cur[-1] <= 12:
            cur.append(x)
        else:
            groups.append(cur)
            cur = [x]
    groups.append(cur)
    for g in groups:
        if (g[-1] - g[0]) >= 40:              # 圆点那条只有十几像素宽，自动被跳过
            return (g[0], ymin, g[-1], ymax)
    return None


def embedded_chrome(root):
    """
    exe 主窗口**里面**的 Chromium 子窗口（= 已经嵌好的浏览器）。

    ★ 不能靠「本机有几个 Chrome_WidgetWin」判断起播没有 —— 这台机器上
      WorkBuddy / QQ / Clash / Qoder 全是 Electron，一数就是三四十个，永远为真。
      只认主窗口的后代。
    """
    out = []
    desc = all_children(root)

    @_CB
    def cb(hwnd, lp):
        cls = ctypes.create_unicode_buffer(128)
        u.GetClassNameW(hwnd, cls, 128)
        if cls.value.startswith("Chrome_WidgetWin"):
            out.append(hwnd)
        return True

    u.EnumChildWindows(root, cb, 0)
    return out


def shot(bbox, name):
    p = os.path.join(SHOT_DIR, name)
    ImageGrab.grab(bbox=bbox, all_screens=True).save(p)
    return p


def main():
    if not os.path.isfile(EXE):
        print("找不到 exe：%s" % EXE)
        return 2
    print("exe:", EXE)
    print("size: %.1f MB" % (os.path.getsize(EXE) / 1048576.0))

    print("== 1) 启动 exe ==")
    proc = subprocess.Popen([EXE], cwd=os.path.dirname(EXE))
    root = find_exe_window(45)
    if not root:
        print("  [!!] 找不到 exe 主窗口")
        try:
            proc.kill()
        except Exception:
            pass
        return 2
    STATE["root"] = root
    info("主窗口 hwnd=%x（启动 %.1fs）" % (root, time.time() - T0))
    threading.Thread(target=monitor, daemon=True).start()

    app_rect = client_rect_on_screen(root)
    info("客户区 = %s（%dx%d）" % (app_rect, app_rect[2] - app_rect[0],
                                   app_rect[3] - app_rect[1]))

    print("== 2) 等频道表加载（本机源）==")
    time.sleep(9)
    img = ImageGrab.grab(bbox=app_rect, all_screens=True)
    shot(app_rect, "exe_embed_0_local.png")
    pill = find_selected_pill(img)
    if not pill:
        check(False, "顶栏里找不到选中的胶囊（截图 exe_embed_0_local.png 看一眼）")
        finish(proc)
        return 2
    info("「本机源」胶囊 bbox（客户区坐标）= %s（宽 %d）" % (pill, pill[2] - pill[0]))

    # 胶囊等宽：下一个胶囊的中心 = 本胶囊右边界 + gap(4) + 半个胶囊宽
    cw = pill[2] - pill[0]
    cx = pill[2] + 4 + cw // 2              # 客户区坐标
    cy = (pill[1] + pill[3]) // 2
    print("== 3) 点「官网源」 @ 客户区 (%d, %d) ==" % (cx, cy))
    switched = None
    for method in ("mouse", "post"):
        CLICK_METHOD[0] = method
        click(root, app_rect, cx, cy)
        time.sleep(2.2)
        shot(app_rect, "exe_embed_1_wvt_%s.png" % method)
        pill2 = find_selected_pill(ImageGrab.grab(bbox=app_rect, all_screens=True))
        if pill2 and pill2[0] > pill[0] + 50:
            switched = method
            break
    info("有效的点击注入方式 = %s" % switched)
    check(bool(switched), "已切到「官网源」（选中胶囊从 x=%s 移到 x=%s）"
          % (pill[0], pill2[0] if pill2 else "?"))
    if not switched:
        finish(proc)
        return 2

    print("== 4) 挨个试频道行，直到内嵌浏览器出现（点一次等最多 7s）==")
    # ★ 不能点一下就 1.6s 换下一个 —— 那等于每 1.6 秒打断一次正在进行的 play()，
    #   是测试脚本自己制造的并发。真实用户是「点一下然后等画面出来」。
    #   所以：点一次，轮询最多 7 秒，看到「已内嵌且可见」才算成功。
    got = 0
    for offset in range(120, 620, 34):
        click(root, app_rect, 130, offset, double=True)
        n_emb = 0
        t1 = time.time()
        while time.time() - t1 < 7.0:
            time.sleep(0.5)
            cand = embedded_chrome(root)
            vis = [h for h in cand if u.IsWindowVisible(h)]
            n_emb = len(cand)
            if vis and client_rect_on_screen(vis[0])[2] - client_rect_on_screen(vis[0])[0] > 100:
                got = n_emb
                break
        info("试 y=%d → 内嵌浏览器窗口数=%d（可见 %d）"
             % (offset, n_emb, len([h for h in embedded_chrome(root)
                                    if u.IsWindowVisible(h)])))
        if got:
            break

    print("== 5) 观察 16s：有没有浏览器跑出来 ==")
    t0 = time.time()
    while time.time() - t0 < 16:
        time.sleep(1.0)
    app_rect2 = client_rect_on_screen(root)
    shot(app_rect2, "exe_embed_2_play.png")

    emb = embedded_chrome(root)
    info("嵌进 exe 窗口里的浏览器窗口数 = %d" % len(emb))
    for hwnd in emb:
        info("    hwnd=%x 可见=%s 窗口矩形=%s 客户区=%s"
             % (hwnd, bool(u.IsWindowVisible(hwnd)),
                _rect(hwnd), client_rect_on_screen(hwnd)))

    print("== 6) 结论 ==")
    check(bool(emb), "官网源确实起了内嵌浏览器（扫描命中 %d 个）" % len(emb))
    # ★ 正面证明「它现在是子窗口」：EnumWindows 只枚举顶层窗口，内嵌成功后
    #   这个 hwnd 必须从顶层列表里消失。还在列表里 = 它又跑出来了。
    top = set(h for h, _r, _v, _i in chrome_windows(set()))
    leaked_top = [h for h in emb if h in top]
    if leaked_top:
        check(False, "已内嵌的窗口仍出现在顶层窗口列表里（%d 个）"
              % len(leaked_top))
    else:
        check(True, "已内嵌的窗口不再出现在顶层窗口列表里（%d 个，证明它已是子窗口）"
              % len(emb))
    if STRAY:
        info("★ 抓到 %d 次「浏览器压在屏幕上且不在 exe 窗口里」：" % len(STRAY))
        for t, hw, rect in STRAY[:10]:
            info("    t=%.2fs hwnd=%s rect=%s" % (t, hw, rect))
        check(False, "全程没有浏览器游离在 exe 窗口之外（露脸 %d 次）" % len(STRAY))
    else:
        info("全程没抓到（采样 20 次/秒）")
        check(True, "全程没有浏览器游离在 exe 窗口之外")

    finish(proc)


def finish(proc):
    print()
    # ★ 这里不能再用 check() —— 它在有失败项时会把「全部检查通过」本身也记成一条
    #   失败，输出就变成自相矛盾的「[!!] 全部检查通过」，把真正的失败项淹掉。
    if not FAIL:
        print("[OK] 全部检查通过 ✅")
    print("总用时 %.1fs" % (time.time() - T0))
    if FAIL:
        print("失败 %d 项：" % len(FAIL))
        for f in FAIL:
            print("  - " + f)
    STOP.set()
    time.sleep(0.2)
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    except Exception:
        pass
    # 顺带清掉可能残留的本项目 Edge（profile 在 exe 同目录的 .browser）
    print("RESULT %s" % ("FAIL" if FAIL else "PASS"))
    sys.stdout.flush()
    os._exit(2 if FAIL else 0)


if __name__ == "__main__":
    sys.exit(main())

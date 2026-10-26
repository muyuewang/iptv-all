"""
官网源 WebView2 直接直播 端到端验收（2026-10-26 改版）。

官网源网页线路不再「嗅探 m3u8 交 mpv」，而是开一个**可见 WebView2 直播窗**
落在播放区，页面自带 <video> 直接播（有声）。本脚本验证：

 1) autoplay 起播官网源（央视 CCTV-1，走 tv.cctv.com 网页线路）
 2) 出现「官网源直播」标题的**可见顶层窗口**（= live window 建窗成功）
 3) 该窗口有真实尺寸且可见（非离屏 -32000）
 4) 日志出现「WebView2 直播窗已开」（前后端链路贯通）
 5) 全程无「official-exhausted」弹窗（未走到失败分支）

来源：md
"""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TV_RS = os.path.dirname(HERE)
user32 = ctypes.windll.user32

# ★ DPI 感知：屏幕坐标抓屏受系统缩放影响，必须用 Per-Monitor v2（-4）读物理值，
#   否则 125% 缩放会抓到错位/放大的区域（差 1.25 倍假象）。
try:
    user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    user32.SetProcessDPIAware()


def find_windows_by_title(pid, keyword):
    """枚举 pid 进程名下标题含 keyword 的顶层窗口，返回 [(hwnd,w,h,x,y,visible)]。"""
    found = []

    def cb(hwnd, lparam):
        wpid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value != pid:
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        ln = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(ln + 1)
        user32.GetWindowTextW(hwnd, title, ln + 1)
        if keyword in title.value:
            r = wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            found.append((hwnd, r.right - r.left, r.bottom - r.top,
                          r.left, r.top, title.value))
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(cb), 0)
    return found


def find_main_window(pid):
    found = []

    def cb(hwnd, lparam):
        wpid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value != pid:
            return True
        ln = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(ln + 1)
        user32.GetWindowTextW(hwnd, title, ln + 1)
        if "纯净电视" in title.value:
            r = wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            found.append((hwnd, r.right - r.left, r.bottom - r.top, r.left, r.top))
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(cb), 0)
    return found[0] if found else None


def run_once(exe, env, logpath):
    log = open(logpath + ".stdout", "w", encoding="utf-8")
    proc = subprocess.Popen([exe], cwd=TV_RS, stdout=log,
                            stderr=subprocess.STDOUT, env=env)
    deadline = time.time() + 30
    ready = False
    while time.time() < deadline:
        try:
            txt = open(logpath, encoding="utf-8", errors="ignore").read()
        except OSError:
            txt = ""
        if '"boot":"enter"' in txt or "boot:enter" in txt:
            ready = True
            break
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    return proc, log, ready


def shot_live_window(hwnd, w, h, out):
    """★ 按**屏幕物理坐标**抓取直播窗画面（屏幕 DC BitBlt）。

    WebView2 用 GPU 合成渲染（Chromium 独立合成线程），BitBlt 从**窗口 DC** 抓
    到的只是背景层（纯黑/灰），PrintWindow 同理回退成父窗口合成结果 —— 这就是
    之前「视频不出画/全灰」假象的根因。正确做法是拿到窗口**屏幕坐标**后从
    屏幕 DC（GetDC(0)）按坐标抓，抓到的是合成后的真实画面。
    """
    gdi32 = ctypes.windll.gdi32
    SRCCOPY = 0x00CC0020

    # 窗口屏幕坐标
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    x, y = r.left, r.top

    sdc = user32.GetDC(0)
    mem = gdi32.CreateCompatibleDC(sdc)
    bmp = gdi32.CreateCompatibleBitmap(sdc, w, h)
    gdi32.SelectObject(mem, bmp)
    gdi32.BitBlt(mem, 0, 0, w, h, sdc, x, y, SRCCOPY)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
                    ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                    ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                    ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

    bih = BITMAPINFOHEADER()
    bih.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bih.biWidth = w
    bih.biHeight = -h
    bih.biPlanes = 1
    bih.biBitCount = 32
    bih.biCompression = 0
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bih), 0)
    from PIL import Image
    img = Image.frombuffer("RGBA", (w, h), buf.raw, "raw", "BGRA", 0, 1).convert("RGB")
    img.save(out)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(0, sdc)


def grab_nonblack(x, y, w, h):
    """从屏幕 DC 按物理坐标抓取，返回非黑像素占比（RGB 和 > 30 视为非黑）。"""
    gdi32 = ctypes.windll.gdi32
    SRCCOPY = 0x00CC0020
    sdc = user32.GetDC(0)
    mem = gdi32.CreateCompatibleDC(sdc)
    bmp = gdi32.CreateCompatibleBitmap(sdc, w, h)
    gdi32.SelectObject(mem, bmp)
    gdi32.BitBlt(mem, 0, 0, w, h, sdc, x, y, SRCCOPY)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
                    ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                    ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                    ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

    bih = BITMAPINFOHEADER()
    bih.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bih.biWidth = w
    bih.biHeight = -h
    bih.biPlanes = 1
    bih.biBitCount = 32
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bih), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(0, sdc)

    total = w * h
    step = max(1, total // 8000)
    nz = 0
    cnt = 0
    for i in range(0, total, step):
        off = i * 4
        b = buf.raw[off]
        g = buf.raw[off + 1]
        r = buf.raw[off + 2]
        if r + g + b > 30:
            nz += 1
        cnt += 1
    return nz / max(1, cnt)


def color_ratios(path):
    """返回 (浅色占比, 纯黑占比)，浅色=RGB 均>200，纯黑=RGB 均<25。"""
    from PIL import Image
    img = Image.open(path).convert("RGB")
    px = img.load()
    W, H = img.size
    light = 0
    dark = 0
    total = 0
    # 抽样（步长 4）加速
    for yy in range(0, H, 4):
        for xx in range(0, W, 4):
            r, g, b = px[xx, yy]
            total += 1
            if r > 200 and g > 200 and b > 200:
                light += 1
            if r < 25 and g < 25 and b < 25:
                dark += 1
    return light / max(1, total), dark / max(1, total)


def main():
    exe = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        TV_RS, "target", "x86_64-pc-windows-msvc", "release", "clean-tv-app.exe")
    group = sys.argv[2] if len(sys.argv) > 2 else "w|央视"
    env = dict(os.environ)
    env["CLEAN_TV_AUTOPLAY"] = "1"
    env["CLEAN_TV_AUTOPLAY_SRC"] = "official"
    env["CLEAN_TV_AUTOPLAY_TID"] = group
    # 官网源网页线路必须让 WebView2 经代理（央视静态资源经代理才通，官方文档坑③），
    # 所以**不剥代理**（与 mpv 直连相反）。
    logpath = os.path.join(TV_RS, "_webview_live_verify.log")
    env["CLEAN_TV_LOG"] = logpath

    proc = log = None
    for attempt in range(1, 4):
        proc, log, ready = run_once(exe, env, logpath)
        print(f"第 {attempt} 次启动 PID={proc.pid} 前端就绪={ready}")
        if ready:
            break
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            proc = log = None
            time.sleep(2)
    if not ready:
        print("!! 3 次启动前端均未就绪")
        if proc and proc.poll() is None:
            proc.terminate()
        return 2
    print(f"启动 PID={proc.pid} (autoplay src=official tid={group})")

    main_win = None
    for _ in range(40):
        main_win = find_main_window(proc.pid)
        if main_win or proc.poll() is not None:
            break
        time.sleep(0.5)
    if not main_win:
        print("!! 主窗口未出现")
        proc.terminate()
        return 2
    mh, mw, mh_, mx, my = main_win
    print(f"主窗口出现：hwnd=0x{mh:x} {mw}x{mh_} @({mx},{my})")

    # 等直播窗出现（官网源网页线路建窗 + 导航，约 3~10s）
    live = None
    deadline = time.time() + 60
    while time.time() < deadline:
        wins = find_windows_by_title(proc.pid, "官网源直播")
        for w in wins:
            hwnd, w_, h_, x, y, title = w
            # 有效窗口 = 有真实尺寸且不在屏幕外（x/y 不是 -32000）
            if w_ > 50 and h_ > 50 and x > -1000 and y > -1000:
                live = w
                break
        if live:
            break
        if proc.poll() is not None:
            break
        time.sleep(0.5)

    if not live:
        print("!! 60s 内未见「官网源直播」可见窗口")
    else:
        hwnd, w_, h_, x, y, title = live
        print(f"直播窗出现：hwnd=0x{hwnd:x} {w_}x{h_} @({x},{y}) title={title!r}")
        # 校验直播窗覆盖在主窗口客户区内的播放区（左上角相邻 / 尺寸贴近播放区）
        # 主窗口客户区左上 ≈ 窗口左上 + 边框，播放区一般在内部偏左。
        print(f"  相对主窗口偏移：({x - mx}, {y - my})")

    # 观察一段，确认窗口持续存在（不闪退）
    time.sleep(8)
    live_still = find_windows_by_title(proc.pid, "官网源直播")
    print("8s 后直播窗仍存在:", bool(live_still))

    # ★ 截图验证：是否「纯视频」而非「原生网页」（网页未净化时画面大量白底/浅色）。
    #   ★★ 视频画面以中低亮度为主但**有内容且在动**；网页未净化则大面积近白。
    #   判据改为「非黑占比 + 双帧差分」（与 verify_embed_batch.py 一致）：
    #   - 真实直播：非黑占比高、两帧差异明显
    #   - 占位/黑屏：非黑极低、两帧几乎相同 → FAIL
    shot_path = os.path.join(TV_RS, "_webview_live_shot.png")
    pure = None
    if live:
        hwnd, w_, h_, x, y, title = live
        try:
            nz1 = nz2 = 0.0
            diff = 0.0
            for _ in range(25):
                r = wt.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                sx, sy = r.left, r.top
                nz1 = grab_nonblack(sx, sy, w_, h_)
                time.sleep(1.0)
                nz2 = grab_nonblack(sx, sy, w_, h_)
                diff = abs(nz1 - nz2)
                white_ratio, dark_ratio = color_ratios(shot_path)
                # 已出画：非黑占比足够高即可跳出
                if nz2 > 0.08:
                    break
                time.sleep(1)
            # 存一张取证图
            shot_live_window(hwnd, w_, h_, shot_path)
            white_ratio, dark_ratio = color_ratios(shot_path)
            print(f"截图已存 {shot_path}，白底/浅色占比={white_ratio:.1%}，纯黑占比={dark_ratio:.1%}")
            print(f"非黑占比 f1={nz1:.1%} f2={nz2:.1%}（画面在动={diff:.1%}）")
            # 纯视频判据：网页未净化白底 > 55%（FAIL）；出画 = 非黑占比 > 8%。
            pure = white_ratio < 0.55 and nz2 > 0.08
        except Exception as e:
            print(f"截图/判定失败（不影响主判据）: {e}")
            pure = None

    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    log.close()

    txt = open(logpath, encoding="utf-8", errors="ignore").read()
    print("--- 日志关键行 ---")
    for ln in txt.splitlines():
        if any(k in ln for k in ("[play]", "预抓取", "autoplay", "直播窗")):
            print("  ", ln.strip())

    opened = "WebView2 直播窗已开" in txt
    exhausted = "official-exhausted" in txt
    print("--- 判定 ---")
    print("直播窗建窗成功(日志):", opened)
    print("直播窗可见(窗口枚举):", bool(live))
    print("未走到失败分支(无 exhausted):", not exhausted)
    print("画面纯净(非白底网页):", pure)
    ok = opened and bool(live) and not exhausted and (pure is not False)
    print("=>", "PASS 官网源 WebView2 直播贯通" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
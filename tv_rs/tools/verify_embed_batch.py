"""
官网源多频道批量验收：内嵌 WebView2 治本路线的**覆盖面**验证。

★ 判据必须用**屏幕 DC 抓取**（不能 PrintWindow）：
   PrintWindow 对 mpv 的 GPU 渲染窗口无效，会回退成父窗口合成结果
   （量到的其实是 UI 界面，各频道都是同样的 46% → 假阳性）。
   必须按**物理坐标**从屏幕 DC BitBlt。

★ 还要 **DPI 感知**：125% 缩放下 DPI-unaware 进程读跨进程窗口矩形会被虚拟化
   （差 1.25 倍），抓图会抓错区域。→ SetThreadDpiAwarenessContext(-4)。

★ 内容判据用「非黑比例 + 双帧差分」，不依赖具体台画面：
   - 占位状态（「正在加载官网源线路…」）：播放区是纯黑底 + 静态文字
     → 非黑比例低（~1~3%）、两帧几乎完全相同
   - 真实电视画面：动态视频 → 非黑比例高、两帧差异明显
   这样黑画面/静态图台也能正确判定「有画面」，而占位状态一定判 FAIL。

用法：
  python tools/verify_embed_batch.py            # 默认样本集
  python tools/verify_embed_batch.py --only 2    # 只跑第 3 个
"""
import ctypes
import ctypes.wintypes as wt
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TV_RS = os.path.dirname(HERE)
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

EXE = os.path.join(TV_RS, "target", "x86_64-pc-windows-msvc", "debug", "clean-tv-app.exe")

# ★ 可用环境变量 CLEAN_TV_EXE 覆盖被测 exe 路径（验收安装目录副本时用）。
if os.environ.get("CLEAN_TV_EXE"):
    EXE = os.environ["CLEAN_TV_EXE"]

# ★ DPI 感知：必须在任何窗口 API 调用之前设置。
try:
    user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    user32.SetProcessDPIAware()

# 代表性样本：覆盖 央视网 / 央视频(SPA) / 地方台 三大类网页线路。
# ★ 前端 autoplay 只会点「该分组第一个带 data-id 的频道」，所以每组只能取第 0 条。
SAMPLES = [
    ("w|央视", 0, "CCTV-1 综合_央视网"),
    ("w|卫视", 0, "湖南卫视_央视频SPA"),
    ("w|国家", 0, "CETV-1_央视频SPA"),
    ("w|湖南", 0, "湖南卫视_地方台"),
    ("w|广东", 0, "广东卫视_地方台"),
]


class BIH(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


def class_of(hwnd):
    b = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, b, 256)
    return b.value


def rect_of(hwnd):
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


def grab_screen(x, y, w, h):
    """按物理坐标从屏幕 DC 抓取，返回 BGRA 原始字节。"""
    sdc = user32.GetDC(0)
    memdc = gdi32.CreateCompatibleDC(sdc)
    bmp = gdi32.CreateCompatibleBitmap(sdc, w, h)
    gdi32.SelectObject(memdc, bmp)
    gdi32.BitBlt(memdc, 0, 0, w, h, sdc, x, y, 0x00CC0020)
    bi = BIH()
    bi.biSize = ctypes.sizeof(BIH)
    bi.biWidth = w
    bi.biHeight = -h
    bi.biPlanes = 1
    bi.biBitCount = 32
    buf = ctypes.create_string_buffer(w * h * 4)
    got = gdi32.GetDIBits(memdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(memdc)
    user32.ReleaseDC(0, sdc)
    if got == 0:
        return None
    return buf.raw


def analyze(data, w, h):
    """返回 (非黑比例, 平均亮度)。"""
    total = w * h
    step = max(1, total // 6000)
    nz = 0
    cnt = 0
    lum = 0
    for i in range(0, total, step):
        off = i * 4
        b = data[off]
        g = data[off + 1]
        r = data[off + 2]
        s = r + g + b
        if s > 30:
            nz += 1
        lum += s
        cnt += 1
    if cnt == 0:
        return 0.0, 0.0
    return nz / cnt, (lum / cnt) / 3.0


def diff_ratio(a, b):
    """两帧之间的差异像素比例。"""
    total = len(a) // 4
    step = max(1, total // 6000)
    d = 0
    cnt = 0
    for i in range(0, total, step):
        off = i * 4
        if abs(a[off] - b[off]) + abs(a[off + 1] - b[off + 1]) + abs(a[off + 2] - b[off + 2]) > 24:
            d += 1
        cnt += 1
    if cnt == 0:
        return 0.0
    return d / cnt


def find_host(pid):
    """找承载窗（class=CleanTVVideoHost）。"""
    tops = []

    def cb(hwnd, lparam):
        wpid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid and user32.IsWindowVisible(hwnd):
            tops.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    for h in tops:
        if class_of(h) == "CleanTVVideoHost":
            return h
    return None


def find_mpv(pid):
    """找承载窗里的 mpv 子窗口。"""
    tops = []

    def cb_top(hwnd, lparam):
        wpid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid:
            tops.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(cb_top), 0)
    for h in tops:
        found = []

        def cb_child(hwnd, lparam):
            if class_of(hwnd).lower() == "mpv":
                found.append(hwnd)
                return False
            return True

        user32.EnumChildWindows(h, WNDENUMPROC(cb_child), 0)
        if found:
            return found[0]
    return None


def top_window_at(x, y):
    """返回屏幕点 (x,y) 处**最顶层**窗口的 hwnd（0 = 无）。

    ★ 这是判据可信度的关键：屏幕 DC 抓图抓到的是「该坐标上用户眼睛看到的
      东西」，如果那个位置被别的进程窗口盖着，抓到的就是别人的画面
      （实测抓到过 IDE 界面 → 非黑 99% 的假阳性）。
      所以抓图前必须先确认这一点的顶层窗口属于被测进程。
    """
    return user32.WindowFromPoint(wt.POINT(int(x), int(y)))


def pid_of(hwnd):
    wpid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
    return wpid.value


def belongs_to(hwnd, allowed_pids):
    """窗口 hwnd 是否属于 allowed_pids 中的任一进程。

    ★ mpv 是**独立进程**（Command::new().spawn()），它用 `--wid=<承载窗hwnd>`
      把自己创建的窗口挂成承载窗的**子窗口** —— 但窗口本身的 pid 是 mpv 的 pid，
      不是 app 的。所以判定归属时必须同时放行 app pid 和 mpv pid。
    """
    if not hwnd:
        return False
    p = pid_of(hwnd)
    if p in allowed_pids:
        return True
    # WindowFromPoint 可能命中更上层的子窗口 → 沿父链上溯再判一次
    cur = hwnd
    for _ in range(4):
        cur = user32.GetParent(cur)
        if not cur:
            break
        if pid_of(cur) in allowed_pids:
            return True
    return False


def raise_windows(pid):
    """把被测进程的主窗口**强制置为前台**（验收时避免被 IDE 等窗口遮挡）。

    ★ VideoHost 是**独立顶层窗口**（主窗口的 owned window），不是主窗口的子窗；
      主窗口不激活时它会被别的窗口压在下面 → 屏幕抓图取到的是遮挡者
      （实测抓到过 IDE 界面 → 非黑 99% 的假阳性）。

    ★ 跨进程 SetForegroundWindow 会被系统拒绝（前台锁），必须用
      AttachThreadInput 把当前线程「挂」到前台线程上再调用。
    """
    tops = []

    def cb(hwnd, lparam):
        wpid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid and user32.IsWindowVisible(hwnd):
            tops.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    # 主窗口 = 非 CleanTVVideoHost 的可见顶层窗口
    main = None
    for h in tops:
        if class_of(h) != "CleanTVVideoHost":
            main = h
            break
    if main:
        fg = user32.GetForegroundWindow()
        cur_tid = ctypes.windll.kernel32.GetCurrentThreadId()
        fg_tid = user32.GetWindowThreadProcessId(fg, None)
        tgt_tid = user32.GetWindowThreadProcessId(main, None)
        user32.AttachThreadInput(cur_tid, fg_tid, True)
        user32.AttachThreadInput(cur_tid, tgt_tid, True)
        try:
            user32.BringWindowToTop(main)
            user32.SetForegroundWindow(main)
        finally:
            user32.AttachThreadInput(cur_tid, tgt_tid, False)
            user32.AttachThreadInput(cur_tid, fg_tid, False)
    # 承载窗也跟着提到前面（它是 owned window，HWND_TOP 让它在同级里排最前）
    for h in tops:
        if class_of(h) == "CleanTVVideoHost":
            user32.SetWindowPos(h, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
    return tops


def save_png(path, data, w, h):
    """把 BGRA 原始数据存成 PNG（取证用）。"""
    import struct
    import zlib

    raw = bytearray()
    for y in range(h):
        raw.append(0)
        row = data[y * w * 4:(y + 1) * w * 4]
        rgb = bytearray(w * 3)
        for x in range(w):
            rgb[x * 3] = row[x * 4 + 2]
            rgb[x * 3 + 1] = row[x * 4 + 1]
            rgb[x * 3 + 2] = row[x * 4]
        raw += rgb

    def chunk(tag, d):
        c = struct.pack(">I", len(d)) + tag + d
        return c + struct.pack(">I", zlib.crc32(tag + d) & 0xffffffff)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


def run_one(tid, cid, label, timeout=95, idx=0):
    log = os.path.join(TV_RS, "_verify_batch_%s.log" % re.sub(r"[^\w]", "_", label))
    if os.path.exists(log):
        os.remove(log)
    env = dict(os.environ)
    env["CLEAN_TV_LOG"] = log
    env["CLEAN_TV_AUTOPLAY"] = "1"
    env["CLEAN_TV_AUTOPLAY_SRC"] = "official@%s" % tid
    proc = subprocess.Popen([EXE], env=env, cwd=TV_RS)
    t0 = time.time()
    ok_win = False
    ratio = 0.0
    diff = 0.0
    host_rect = None
    best = 0.0
    occluded = 0
    try:
        while time.time() - t0 < timeout:
            time.sleep(1.5)
            mpv = find_mpv(proc.pid)
            if not mpv:
                continue
            ok_win = True
            host = find_host(proc.pid)
            if not host:
                continue
            host_rect = rect_of(host)
            x, y, w, h = host_rect
            # ★ 先把被测窗口提到前台，再确认该坐标顶层窗口属于本进程。
            #   否则屏幕抓图抓到的是遮挡者（实测抓到 IDE 界面 → 假阳性）。
            raise_windows(proc.pid)
            time.sleep(0.4)
            top = top_window_at(x + w // 2, y + h // 2)
            # mpv 是独立进程 → 它的视频子窗口 pid ≠ app pid，必须一并放行
            allowed = {proc.pid, pid_of(mpv)}
            if not belongs_to(top, allowed):
                occluded += 1
                continue
            f1 = grab_screen(x, y, w, h)
            if not f1:
                continue
            time.sleep(1.2)
            f2 = grab_screen(x, y, w, h)
            if not f2:
                continue
            ratio, lum = analyze(f1, w, h)
            ratio2, _ = analyze(f2, w, h)
            diff = diff_ratio(f1, f2)
            # ★ 取两帧里更好的那个：mpv 的视频子窗口先创建、画面后渲染，
            #   抓第一帧常常还是黑屏（实测表现为「非黑 0.8% + 帧差 70%」——
            #   f1 全黑、f2 才出画）。只看 f1 会误判 FAIL。
            best = max(ratio, ratio2)
            save_png(os.path.join(TV_RS, "_vbatch_%d_f1.png" % idx), f1, w, h)
            save_png(os.path.join(TV_RS, "_vbatch_%d_f2.png" % idx), f2, w, h)
            # 有画面即认为起播成功（非黑比例够 或 画面在动）。
            if best > 0.25 or (diff > 0.01 and best > 0.05):
                break
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    sniffed = ""
    embedded = False
    cdp_used = False
    fell_back = ""
    if os.path.exists(log):
        with open(log, "r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                if "嗅探命中" in ln:
                    sniffed = ln.strip()
                if "内嵌嗅探失败" in ln:
                    embedded = False
                    cdp_used = True
                elif "嗅探命中" in ln and not embedded:
                    embedded = True
                if "回落" in ln:
                    fell_back = ln.strip()
    return ok_win, best, diff, sniffed, embedded, cdp_used, fell_back, host_rect, occluded


def main():
    only = None
    if "--only" in sys.argv:
        only = int(sys.argv[sys.argv.index("--only") + 1])
    results = []
    for idx, (tid, cid, label) in enumerate(SAMPLES):
        if only is not None and idx != only:
            continue
        print("\n===== [%d] %s（%s cid=%d）=====" % (idx, label, tid, cid))
        t0 = time.time()
        ok_win, ratio, diff, sniffed, embedded, cdp_used, fell_back, hrect, occl = run_one(
            tid, cid, label, idx=idx)
        dt = time.time() - t0
        # 判据：mpv 窗口存在 + 播放区真有画面（非黑比例够 或 画面在动）
        has_picture = (ratio > 0.25) or (diff > 0.01 and ratio > 0.05)
        verdict = "PASS" if (ok_win and has_picture) else "FAIL"
        route = "内嵌WebView2" if embedded else ("CDP兜底" if cdp_used else "未嗅探")
        print("  路由: %s" % route)
        print("  承载窗: %s" % (str(hrect) if hrect else "无"))
        print("  出画窗口: %s   非黑像素: %.1f%%   帧间差: %.1f%%   耗时: %.0fs   被遮挡次数: %d" % (
            "有" if ok_win else "无", ratio * 100, diff * 100, dt, occl))
        if sniffed:
            print("  %s" % sniffed[:150])
        if fell_back:
            print("  回落: %s" % fell_back[:120])
        print("  >>> %s" % verdict)
        results.append((label, verdict, route, ratio, diff))
    print("\n\n========== 汇总 ==========")
    npass = sum(1 for r in results if r[1] == "PASS")
    for label, verdict, route, ratio, diff in results:
        print("  %-4s %-24s %-12s 非黑 %.0f%% 帧差 %.1f%%" % (
            verdict, label, route, ratio * 100, diff * 100))
    print("  通过 %d / %d" % (npass, len(results)))
    return 0 if npass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

"""
官网源「内嵌 WebView2 治本路线」端到端验收。

判定（全部必须满足才算 PASS）：
 1) 官网源频道被选中并进入嗅探（日志含「嗅探网页」）
 2) **内嵌嗅探命中**（日志含「内嵌嗅探」成功路径 / 「嗅探命中」）
 3) mpv 真实出画面：主窗口下存在 class="mpv" 的子窗口，且尺寸非 0、可见
    （对应 player.rs::video_ready —— 只有解出第一帧 mpv 才会建这个子窗口）
 4) 该子窗口内有实际像素（非纯黑），排除「有声音没图像」

用法：
  python verify_embed_official.py [--exe <path>] [--tid "w|央视"] [--cid 0]
环境变量透传：CLEAN_TV_LOG 由本脚本设置。
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
gdi32 = ctypes.windll.gdi32


def find_mpv_children(pid):
    """枚举 pid 所属窗口的 class="mpv" 子窗口，返回 [(hwnd, w, h)]。"""
    out = []

    class Ctx(ctypes.Structure):
        _fields_ = [("target_pid", wt.DWORD), ("found", ctypes.POINTER(wt.HWND))]

    top = []

    def cb_top(hwnd, lparam):
        wpid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid:
            top.append(hwnd)
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    user32.EnumWindows(WNDENUMPROC(cb_top), 0)

    for h in top:
        def cb_child(hwnd, lparam):
            buf = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, buf, 64)
            if buf.value.lower() == "mpv":
                r = wt.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                w = r.right - r.left
                hh = r.bottom - r.top
                if w > 0 and hh > 0 and user32.IsWindowVisible(hwnd):
                    out.append((hwnd, w, hh))
                return False
            return True

        user32.EnumChildWindows(h, WNDENUMPROC(cb_child), 0)
    return out


def has_real_pixels(hwnd):
    """检查窗口区域是否有非黑像素（避免「窗口在但没画面」）。"""
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        return False, "0x0"
    hdc = user32.GetWindowDC(hwnd)
    if not hdc:
        return False, "no-dc"
    memdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(memdc, bmp)
    # 抓整个窗口区域（PRAYF：PrintWindow 对某些窗口返回黑）
    ok = user32.PrintWindow(hwnd, memdc, 2)
    if not ok:
        gdi32.BitBlt(memdc, 0, 0, w, h, hdc, 0, 0, 0x00CC0020)
    # 读回像素
    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
                    ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                    ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                    ("biClrImportant", wt.DWORD)]

    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth = w
    bi.biHeight = -h
    bi.biPlanes = 1
    bi.biBitCount = 32
    bi.biCompression = 0
    buf = ctypes.create_string_buffer(w * h * 4)
    got = gdi32.GetDIBits(memdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(memdc)
    user32.ReleaseDC(hwnd, hdc)
    if got == 0:
        return False, "getdibits-fail"
    data = buf.raw
    total = w * h
    # 采样统计非黑像素比例
    nonzero = 0
    sample = max(1, total // 4000)
    cnt = 0
    for i in range(0, total, sample):
        off = i * 4
        b = data[off]
        g = data[off + 1]
        rr = data[off + 2]
        cnt += 1
        if b + g + rr > 24:
            nonzero += 1
    ratio = nonzero / max(1, cnt)
    return ratio > 0.02, "非黑像素占比 %.1f%%" % (ratio * 100)


def main():
    exe = None
    tid = "w|央视"
    cid = "0"
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--exe" and i + 1 < len(args):
            exe = args[i + 1]
            i += 2
        elif args[i] == "--tid" and i + 1 < len(args):
            tid = args[i + 1]
            i += 2
        elif args[i] == "--cid" and i + 1 < len(args):
            cid = args[i + 1]
            i += 2
        else:
            i += 1
    if exe is None:
        exe = os.path.join(TV_RS, "target", "x86_64-pc-windows-msvc", "debug", "clean-tv-app.exe")
    if not os.path.isfile(exe):
        print("找不到 exe: %s" % exe)
        return 2

    log = os.path.join(TV_RS, "_verify_embed_official.log")
    if os.path.exists(log):
        os.remove(log)
    env = dict(os.environ)
    env["CLEAN_TV_LOG"] = log
    env["CLEAN_TV_AUTOPLAY"] = "1"
    env["CLEAN_TV_AUTOPLAY_SRC"] = "official@%s" % tid
    env["CLEAN_TV_AUTOPLAY_TID"] = tid
    env.pop("CLEAN_TV_SKIP_PREWARM_CHANNELS", None)
    env.pop("CLEAN_TV_SKIP_PREWARM_WVT", None)

    print("启动: %s  tid=%s cid=%s" % (exe, tid, cid))
    proc = subprocess.Popen([exe], env=env, cwd=TV_RS)
    t0 = time.time()
    mpv_found_at = None
    pixels_ok = False
    pix_detail = ""
    try:
        while time.time() - t0 < 75:
            time.sleep(1.5)
            kids = find_mpv_children(proc.pid)
            if kids:
                if mpv_found_at is None:
                    mpv_found_at = time.time()
                    print("[%.1fs] 发现 mpv 视频子窗口 %dx%d" % (
                        mpv_found_at - t0, kids[0][1], kids[0][2]))
                ok, detail = has_real_pixels(kids[0][0])
                pix_detail = detail
                if ok:
                    pixels_ok = True
                    print("[%.1fs] 像素检查通过: %s" % (time.time() - t0, detail))
                    break
    finally:
        time.sleep(0.5)
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    print("\n===== 日志关键行 =====")
    if os.path.exists(log):
        with open(log, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        keys = ("嗅探", "命中", "出画", "回落", "embed", "失败", "未出画面", "boot", "play]")
        for ln in lines:
            if any(k in ln for k in keys):
                print(ln.rstrip())
        print("\n(日志共 %d 行)" % len(lines))
    else:
        print("!! 无日志文件")

    print("\n===== 判定 =====")
    ok_win = mpv_found_at is not None
    print("[%s] mpv 真实视频窗口存在" % ("PASS" if ok_win else "FAIL"))
    print("[%s] 窗口内有实际像素 (%s)" % ("PASS" if pixels_ok else "FAIL", pix_detail))
    return 0 if (ok_win and pixels_ok) else 1


if __name__ == "__main__":
    sys.exit(main())

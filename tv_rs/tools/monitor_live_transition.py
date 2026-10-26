"""
逐帧监视「官网源直播窗」从建窗到出画的过渡过程（2026-10-26）。

要回答两个问题（用户实测反馈）：
  1. 建窗瞬间窗口会不会在**错误位置**闪一下（屏幕左上角）？
     → 每帧记录窗口 rect，看是否出现过非播放区坐标。
  2. 加载期（<video> 还没出画）画面是不是**全黑**、看不到网页？
     → 每帧算非黑占比；出画前应 ≈0%，出画后跳到几十个百分点。

用法：
  python tools/monitor_live_transition.py            # 默认监控 12 秒
  python tools/monitor_live_transition.py --secs 15
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

try:
    user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    user32.SetProcessDPIAware()

EXE = os.path.join(TV_RS, "target", "x86_64-pc-windows-msvc", "release", "clean-tv-app.exe")
if os.environ.get("CLEAN_TV_EXE"):
    EXE = os.environ["CLEAN_TV_EXE"]


class BIH(ctypes.Structure):
    _fields_ = [("s", wt.DWORD), ("w", ctypes.c_long), ("h", ctypes.c_long),
                ("p", wt.WORD), ("bc", wt.WORD), ("cc", wt.DWORD), ("si", wt.DWORD),
                ("xp", ctypes.c_long), ("yp", ctypes.c_long),
                ("cu", wt.DWORD), ("ci", wt.DWORD)]


def find_live(pid):
    """返回 (hwnd, x, y, w, h, visible, layered) 或 None。"""
    found = []

    def cb(hwnd, lp):
        wpid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value != pid:
            return True
        ln = user32.GetWindowTextLengthW(hwnd)
        if ln <= 0:
            return True
        b = ctypes.create_unicode_buffer(ln + 1)
        user32.GetWindowTextW(hwnd, b, ln + 1)
        if "官网源直播" not in b.value:
            return True
        r = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        ex = user32.GetWindowLongW(hwnd, -20)
        found.append((hwnd, r.left, r.top, r.right - r.left, r.bottom - r.top,
                      bool(user32.IsWindowVisible(hwnd)), bool(ex & 0x80000)))
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)(cb), 0)
    return found[0] if found else None


def nonblack(x, y, w, h):
    """屏幕 DC 抓取 → 非黑占比（RGB 和 > 30 视为非黑）。"""
    if w <= 0 or h <= 0:
        return None
    sdc = user32.GetDC(0)
    mem = gdi32.CreateCompatibleDC(sdc)
    bmp = gdi32.CreateCompatibleBitmap(sdc, w, h)
    gdi32.SelectObject(mem, bmp)
    ok = gdi32.BitBlt(mem, 0, 0, w, h, sdc, x, y, 0x00CC0020)
    if not ok:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(0, sdc)
        return None
    bi = BIH()
    bi.s = ctypes.sizeof(BIH)
    bi.w = w
    bi.h = -h
    bi.p = 1
    bi.bc = 32
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(0, sdc)

    data = buf.raw
    total = w * h
    step = max(1, total // 4000)
    nz = 0
    cnt = 0
    for i in range(0, total, step):
        off = i * 4
        if data[off] + data[off + 1] + data[off + 2] > 30:
            nz += 1
        cnt += 1
    return nz / max(1, cnt)


def main():
    secs = 12.0
    if "--secs" in sys.argv:
        secs = float(sys.argv[sys.argv.index("--secs") + 1])

    env = dict(os.environ)
    env["CLEAN_TV_AUTOPLAY"] = "1"
    env["CLEAN_TV_AUTOPLAY_SRC"] = "official"
    env["CLEAN_TV_AUTOPLAY_TID"] = "w|央视"
    env["CLEAN_TV_AUTOPLAY_CID"] = "0"
    env["CLEAN_TV_LOG"] = os.path.join(TV_RS, "_monitor_transition.log")

    proc = subprocess.Popen([EXE], env=env, cwd=TV_RS)
    print(f"启动 PID={proc.pid}  监控 {secs:.0f}s（每 120ms 采样）\n")
    print(f"{'t(s)':>6} {'窗口':>4} {'rect':>24} {'层':>3} {'非黑':>7}  备注")
    print("-" * 76)

    t0 = time.time()
    rows = []
    bad_pos = 0
    while time.time() - t0 < secs:
        w = find_live(proc.pid)
        t = time.time() - t0
        if not w:
            rows.append((t, None, None))
            print(f"{t:6.2f} {'无':>4} {'-':>24} {'-':>3} {'-':>7}  未建窗")
        else:
            hwnd, x, y, ww, hh, vis, layered = w
            nb = nonblack(x, y, ww, hh)
            rect = f"({x},{y}) {ww}x{hh}"
            # 播放区固定为 (702,164)。屏幕外 (-32000,-32000) 是**预期**的「等出画」状态：
            # 窗口在屏幕外渲染（Chromium 认为可见，播放器照常起播），用户看不到。
            # 真正的异常是「出现在播放区以外的可见位置」（旧版那种错位闪现）。
            offscreen = x <= -30000 or y <= -30000
            misplaced = (not offscreen) and not (x == 702 and y == 164)
            if misplaced:
                bad_pos += 1
            note = []
            if offscreen:
                note.append("屏幕外等待")
            if misplaced:
                note.append("★位置异常")
            if not vis:
                note.append("不可见")
            if layered:
                note.append("layered")
            print(f"{t:6.2f} {'有':>4} {rect:>24} {'是' if vis else '否':>3} "
                  f"{nb*100:6.1f}%  {' '.join(note)}")
            rows.append((t, (x, y, ww, hh), nb))
        time.sleep(0.12)

    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass

    # 统计
    seen = [r for r in rows if r[1]]
    print("\n" + "=" * 76)
    if not seen:
        print("!! 全程未出现直播窗")
        return 2
    first = seen[0]
    print(f"首次出现：t={first[0]:.2f}s  rect={first[1]}")
    print(f"位置异常帧数（不含屏幕外等待）：{bad_pos} / {len(seen)}")
    vals = [(t, nb) for t, rect, nb in rows if nb is not None]
    if vals:
        dark_frames = [t for t, nb in vals if nb < 0.02]
        draw_frames = [(t, nb) for t, nb in vals if nb >= 0.10]
        print(f"全黑帧（非黑<2%）：{len(dark_frames)} 个"
              f"{f'，时间 {min(dark_frames):.2f}~{max(dark_frames):.2f}s' if dark_frames else ''}")
        if draw_frames:
            print(f"出画首帧：t={draw_frames[0][0]:.2f}s  非黑={draw_frames[0][1]*100:.1f}%")
        else:
            print("!! 全程未出画")
    print("=" * 76)
    return 0


if __name__ == "__main__":
    sys.exit(main())

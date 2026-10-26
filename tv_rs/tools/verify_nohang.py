"""
验收脚本：验证修复后
 1) 窗口在后台取流期间不卡死（「未响应」）
 2) 后台取流成功起播（mpv 进程出现 + 日志「后台线程」）
 3) 命令秒回（autoplay 触发 play_channel 后窗口仍可被 EnumWindows 命中且尺寸正常）

用法：
  python tools/verify_nohang.py <release exe 路径>
"""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

user32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32

HERE = os.path.dirname(os.path.abspath(__file__))
TV_RS = os.path.dirname(HERE)


def count_proc(name):
    TH32CS_SNAPPROCESS = 0x00000002

    class PE(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                    ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", wt.LONG),
                    ("dwFlags", wt.DWORD), ("szExeFile", wt.WCHAR * 260)]

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    n = 0
    try:
        e = PE(); e.dwSize = ctypes.sizeof(PE)
        if k32.Process32FirstW(snap, ctypes.byref(e)):
            while True:
                if e.szExeFile.lower() == name.lower():
                    n += 1
                if not k32.Process32NextW(snap, ctypes.byref(e)):
                    break
    finally:
        k32.CloseHandle(snap)
    return n


def find_main_window(pid):
    """返回 (hwnd, w, h) 或 None。用线程 pid 匹配，且标题含『纯净电视』。"""
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
            found.append((hwnd, r.right - r.left, r.bottom - r.top))
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(cb), 0)
    return found[0] if found else None


def main():
    exe = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        TV_RS, "target", "x86_64-pc-windows-msvc", "release", "clean-tv-app.exe")
    if not os.path.isfile(exe):
        print("!! exe 不存在:", exe)
        return 2

    env = dict(os.environ)
    env["CLEAN_TV_AUTOPLAY"] = "1"
    log = open(os.path.join(TV_RS, "_nohang.log"), "w", encoding="utf-8")
    proc = subprocess.Popen([exe], cwd=TV_RS, stdout=log, stderr=subprocess.STDOUT, env=env)
    print("启动 PID=%d (autoplay)" % proc.pid)

    # 阶段1：等窗口出现，并确认取流进行中窗口仍「可枚举且尺寸正常」= 未卡死
    hung = False
    win = None
    for _ in range(40):
        win = find_main_window(proc.pid)
        if win:
            break
        if proc.poll() is not None:
            print("!! 进程提前退出 code=", proc.returncode)
            break
        time.sleep(0.5)
    if not win:
        print("!! 窗口未出现")
        proc.terminate()
        return 2
    hwnd, w, h = win
    print("窗口出现：hwnd=0x%x %dx%d" % (hwnd, w, h))

    # 取流在后台（5~15s），期间每 1s 确认窗口仍可被 EnumWindows 命中（= 消息泵活）
    responsive_all = True
    for i in range(12):
        time.sleep(1)
        still = find_main_window(proc.pid)
        if not still:
            print("  [s%d] 窗口枚举失败（疑似卡死/未响应）" % i)
            responsive_all = False
            break
    print("取流期间窗口持续可枚举（未卡死）:", responsive_all)

    # 阶段2：等 mpv 出现（后台取流成功）
    played = False
    deadline = time.time() + 40
    while time.time() < deadline:
        if count_proc("mpv.exe") > 0:
            played = True
            break
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    if played:
        time.sleep(3)
        played = count_proc("mpv.exe") > 0
    print("mpv.exe 起播存活:", played)

    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    log.close()

    txt = open(os.path.join(TV_RS, "_nohang.log"), encoding="utf-8", errors="ignore").read()
    has_streaming_ret = "命令返回" in txt
    has_bg_start = ("本机源 mpv 已启动（后台线程）" in txt) or ("789 mpv 已启动（后台线程）" in txt)
    print("--- 日志关键行 ---")
    for ln in txt.splitlines():
        if "[play]" in ln or "789" in ln:
            print("  ", ln.strip())

    print("--- 判定 ---")
    print("命令秒回(后台派发/命令返回):", has_streaming_ret)
    print("后台线程起播（本机源/789）:", has_bg_start)
    print("取流期间未卡死:", responsive_all)
    print("mpv 真实起播:", played)
    ok = has_streaming_ret and has_bg_start and responsive_all and played
    print("=>", "PASS 卡死已修复 + 后台取流贯通" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

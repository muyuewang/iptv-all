"""
端到端验收（本机源 / 官网源分组通用）。

用法：
  python tools/verify_official.py <exe> [tid]
    tid 缺省 "ys"（本机源央视）；官网源分组写 "w|央视"

判定：
 1) 分类/分组预抓成功（本机源「预抓取 ys 完成」/ 官网源「解密 … 分组」）
 2) autoplay 真的触发并走到 play_channel（前端 WebView2 偶发静默 flaky → 自动重启重试）
 3) 取流期间窗口持续可枚举（未卡死）
 4) mpv 真起播：以 **app 自证心跳**（8s 后 child 仍存活）为准——本机 Python 侧
    进程枚举不可靠（subprocess 返回 None / Toolhelp 数不到 mpv，tasklist 能看到）。
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
k32 = ctypes.windll.kernel32

LOGPATH = os.path.join(TV_RS, "_official_verify.log")


def read_log():
    try:
        return open(LOGPATH, encoding="utf-8", errors="ignore").read()
    except OSError:
        return ""


def count_proc(name):
    TH32CS_SNAPPROCESS = 0x00000002

    class PE(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                    ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", ctypes.c_void_p), ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wt.DWORD), ("szExeFile", wt.WCHAR * 260)]

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    n = 0
    try:
        e = PE()
        e.dwSize = ctypes.sizeof(PE)
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
            w, h = r.right - r.left, r.bottom - r.top
            if w > 400 and h > 300:      # 排除 159x27 之类的小窗/提示窗
                found.append((hwnd, w, h))
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(cb), 0)
    return found[0] if found else None


def launch(exe, env):
    if os.path.exists(LOGPATH):
        os.remove(LOGPATH)
    log = open(LOGPATH + ".stdout", "w", encoding="utf-8")
    proc = subprocess.Popen([exe], cwd=TV_RS, stdout=log, stderr=subprocess.STDOUT, env=env)
    return proc, log


def wait_until(fn, timeout, step=0.5):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if fn():
            return True
        time.sleep(step)
    return False


def kill(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def main():
    exe = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        TV_RS, "target", "x86_64-pc-windows-msvc", "release", "clean-tv-app.exe")
    tid = sys.argv[2] if len(sys.argv) > 2 else "ys"

    env = dict(os.environ)
    env["CLEAN_TV_AUTOPLAY"] = "1"
    env["CLEAN_TV_AUTOPLAY_TID"] = tid
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        env.pop(k, None)
    env["CLEAN_TV_LOG"] = LOGPATH
    env["CLEAN_TV_MPV_LOG"] = os.path.join(TV_RS, "_official_mpv.log")

    proc = log = None
    win = None
    for attempt in range(1, 4):
        proc, log = launch(exe, env)
        ready = wait_until(lambda: "boot:enter" in read_log(), 30)
        print(f"第 {attempt} 次启动 PID={proc.pid} 前端就绪={ready} tid={tid}")
        if ready:
            win = None
            for _ in range(40):
                win = find_main_window(proc.pid)
                if win:
                    break
                time.sleep(0.5)
            # ★ 关键：等 autoplay 真的走到 play_channel（WebView2 偶发静默不执行 JS，
            #   表现为 boot:enter 有、分类也拉了，但 autoplay 轮询没跑）。
            if wait_until(lambda: "play_channel" in read_log(), 30):
                break
            print("  !! 未触发 play_channel（前端静默 flaky）→ 重启重试")
        kill(proc)
        proc = None
        time.sleep(2)

    if proc is None or proc.poll() is not None:
        print("!! 3 次启动均未进入播放流程")
        return 2
    if not win:
        win = find_main_window(proc.pid)
    print("窗口：", win)

    # 3) 取流期间窗口持续可枚举（卡死检测）
    responsive = True
    for i in range(12):
        time.sleep(1)
        if not find_main_window(proc.pid):
            print(f"  [s{i}] 窗口枚举失败（疑似卡死）")
            responsive = False
            break
    print("抓线期间窗口持续可枚举（未卡死）:", responsive)

    # 4) 等自证心跳（play 后 8s 落盘）或 mpv 退出记录
    wait_until(lambda: ("mpv 存活 >8s" in read_log()) or ("mpv 退出" in read_log()), 60)
    txt = read_log()

    lived = [float(m) for m in re.findall(r"存活 (\d+\.\d)s", txt)]
    heart = "mpv 存活 >8s（起播确认）" in txt
    played = heart or any(v >= 8 for v in lived) or count_proc("mpv.exe") > 0
    print("mpv 起播存活:", played, "(自证心跳)" if heart else "")

    kill(proc)
    log.close()

    txt = read_log()
    print("--- 日志关键行 ---")
    for ln in txt.splitlines():
        if any(k in ln for k in ("[play]", "[fallback]", "autoplay")):
            print("  ", ln.strip())

    if tid.startswith("w|"):
        cats_ok = "分组" in txt and "解密" in txt
    else:
        cats_ok = (f"预抓取 {tid} 完成" in txt) or ("本机源频道预抓取结束" in txt)
    play_ok = "play_channel" in txt and (played or "已派发" in txt or "派发后台" in txt)

    print("--- 判定 ---")
    print("分类/分组预抓成功:", cats_ok)
    print("播放流程触发:", "play_channel" in txt)
    print("未卡死:", responsive)
    print("mpv 真实起播:", played)
    ok = cats_ok and responsive and played
    print("=>", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

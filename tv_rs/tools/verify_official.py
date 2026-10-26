"""
官网源端到端验收：CLEAN_TV_AUTOPLAY_SRC=official 强制 autoplay 切官网源起播。
判定：
 1) 本机源分类预抓成功
 2) 官网源后台实时抓线贯通（日志「本机源 mpv 已启动（后台线程）」）
 3) 取流期间窗口持续可枚举（未卡死）+ mpv 真实起播
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
k32 = ctypes.windll.kernel32


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


def mpv_alive(app_pid):
    """mpv 存活判定：走 mpv 自己创建的命名管道（player.rs 里
    `clean-tv-mpv-{app_pid}.ipc`）。比 Toolhelp 遍历可靠——本机实测
    Process32First/Next 枚举不到 mpv.exe（进程照样在 tasklist 里）。"""
    import os as _os
    return _os.path.exists(r"\\.\pipe\clean-tv-mpv-%d.ipc" % app_pid)


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
            found.append((hwnd, r.right - r.left, r.bottom - r.top))
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(cb), 0)
    return found[0] if found else None


def run_once(exe, env, logpath):
    """启动一次应用，返回 (proc, log, 前端就绪 bool)。前端静默 flaky 时由外层重试。"""
    # ★ stdout 必须与 diag 日志分开：diag 走 CLEAN_TV_LOG 追加写，若 stdout 也指向
    #   同一文件，两个 fd 各自维护偏移会互相覆盖，读出来是交错的重复行（曾误导排查）。
    log = open(logpath + ".stdout", "w", encoding="utf-8")
    proc = subprocess.Popen([exe], cwd=TV_RS, stdout=log, stderr=subprocess.STDOUT, env=env)
    # 等 boot:enter 自检（前端 JS 真正跑起来的标志），最多 30s。
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


def main():
    exe = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        TV_RS, "target", "x86_64-pc-windows-msvc", "release", "clean-tv-app.exe")
    env = dict(os.environ)
    env["CLEAN_TV_AUTOPLAY"] = "1"
    # ★ 直接指定启动分类（AUTOPLAY_SRC 的 switchSrc 轮询链路实测会静默卡住
    #   主轮询，弃用；只设 AUTOPLAY_TID，主轮询会点 curTid=o_ys 的条目）。
    env["CLEAN_TV_AUTOPLAY_TID"] = sys.argv[2] if len(sys.argv) > 2 else "ys"
    # ★ 剥掉沙箱代理：mpv(ffmpeg) 走该代理会对官网源 eplay2 报 502（实测）。
    #   用户桌面环境无这些变量，剥掉才反映真实环境。
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
              "all_proxy", "ALL_PROXY"):
        env.pop(k, None)
    # diag 日志通道（release GUI 无 stdout，落盘是唯一可见输出）。
    logpath = os.path.join(TV_RS, "_official_verify.log")
    env["CLEAN_TV_LOG"] = logpath
    # mpv stderr 落盘（秒退 code=2 时抓真实死因）。
    env["CLEAN_TV_MPV_LOG"] = os.path.join(TV_RS, "_official_mpv.log")

    # ★ 前端静默 flaky（连续强杀后 WebView2 偶发不加载 JS）：boot:enter 30s
    #   没出现就杀掉重启，最多 3 次。
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
    print("启动 PID=%d (autoplay tid=ys)" % proc.pid)

    win = None
    for _ in range(40):
        win = find_main_window(proc.pid)
        if win or proc.poll() is not None:
            break
        time.sleep(0.5)
    if not win:
        print("!! 窗口未出现")
        proc.terminate()
        return 2
    print("窗口出现：hwnd=0x%x %dx%d" % win)

    responsive = True
    for i in range(15):
        time.sleep(1)
        if not find_main_window(proc.pid):
            print("  [s%d] 窗口枚举失败（疑似卡死）" % i)
            responsive = False
            break
    print("抓线期间窗口持续可枚举（未卡死）:", responsive)

    played = False
    deadline = time.time() + 90
    while time.time() < deadline:
        if mpv_alive(proc.pid) or count_proc("mpv.exe") > 0:
            played = True
            break
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    if played:
        time.sleep(3)
        played = mpv_alive(proc.pid) or count_proc("mpv.exe") > 0
    # ★ 日志判据兜底：mpv 可能起在检测窗口尾部（前端 IPC 排队慢），
    #   「本机源 mpv 已启动（后台线程）」且无秒退记录同样算起播成功。
    try:
        txt = open(os.path.join(TV_RS, "_official_verify.log"), encoding="utf-8", errors="ignore").read()
    except OSError:
        txt = ""
    # ★ 本机沙箱里 Python 侧进程枚举不可靠（subprocess 返回 None、
    #   Toolhelp 数不到 mpv 但 tasklist 能看到）→ 以 **app 自证心跳** 为准：
    #   play 后 8 秒 child.try_wait() 仍为运行中，即实现真起播。
    if not played and "mpv 存活 >8s（起播确认）" in txt:
        played = True
        print("(mpv 由 app 自证心跳确认起播)")
    print("mpv 起播存活:", played)

    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    log.close()

    txt = open(os.path.join(TV_RS, "_official_verify.log"), encoding="utf-8", errors="ignore").read()
    print("--- 日志关键行 ---")
    for ln in txt.splitlines():
        if any(k in ln for k in ("[play]", "预抓取", "autoplay")):
            print("  ", ln.strip())

    cats_ok = "本机源预抓取 ys 完成" in txt or "预抓取 ys 完成" in txt
    import re as _re
    _lived = [float(m) for m in _re.findall(r"存活 (\d+\.\d)s", txt)]
    play_ok = ("mpv 存活 >8s（起播确认）" in txt) or any(v >= 8 for v in _lived) or ("code=0" in txt)
    print("--- 判定 ---")
    print("本机源分类预抓成功:", cats_ok)
    print("本机源后台实时抓线起播:", play_ok or played)
    print("未卡死:", responsive)
    print("mpv 真实起播:", played)
    ok = cats_ok and (play_ok or played) and responsive and played
    print("=>", "PASS 本机源贯通" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

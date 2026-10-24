"""
一次调用内完成 Tauri 壳的运行验证：启动 -> 等窗口 -> 截图 -> 收尾。

为什么需要这个脚本：本环境里"后台起进程"和"后续命令"分成两次工具调用时，
进程会被回收，且 `cd X && cmd &` 这种写法会让整行落在后台子 shell，
导致后续调用的 cwd 与预期不符。把全流程收进一个进程内最可靠。

★★ 判读规则（重要，别被截图骗了）：
  WebView2 的内容走 DirectComposition 硬件合成层，**不进 GDI 屏幕表面**。
  所以 PrintWindow(PW_RENDERFULLCONTENT) 和屏幕 BitBlt 都只能抓到纯白窗口
  背景 —— 这是引擎级限制，不是本脚本的 bug，截出白图 ≠ 页面白屏。

  真正可信的判据是进程日志里的 `[selfcheck] {...}` 一行（由页面主世界调用
  self_check 命令上报），它包含 title / bodyBgImage / h1Text / h1Box /
  liCount / dpr / viewport，能同时证明：页面导航成功、JS 执行、CSS 生效、
  布局算出尺寸、serde 跨 IPC 传值正常。

验证三件事：
  1. 进程能起来并存活（MSVC + WebView2 运行时 OK）
  2. 顶层窗口真的创建出来（标题正确）
  3. `[selfcheck]` 日志出现（页面真实渲染，见上面判读规则）

用法：
  python tools/verify_app.py [exe路径] [截图输出路径]
"""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

PW_RENDERFULLCONTENT = 0x00000002
HERE = os.path.dirname(os.path.abspath(__file__))
TV_RS = os.path.dirname(HERE)


def find_windows_by_pid(pid):
    """枚举属于指定 PID 的所有顶层窗口。"""
    rows = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        wpid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value != pid:
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        r = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        rows.append({
            "hwnd": hwnd,
            "title": buf.value,
            "cls": cls.value,
            "visible": bool(user32.IsWindowVisible(hwnd)),
            "size": (r.right - r.left, r.bottom - r.top),
        })
        return True

    user32.EnumWindows(cb, 0)
    return rows


def count_webview2_children(parent_pid):
    """统计 WebView2 子进程数（按父 PID 过滤，避免统计到别的应用）。

    ★ 不用 `wmic`：Windows 11 新版本已把 WMIC 移除（本机 `System32\\wbem\\WMIC.exe`
      都不在），调用必然失败并静默返回 -1，看着像「0 个子进程」实则是没测到。
      这里走 Toolhelp32 快照，纯 ctypes、无外部依赖。
    """
    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
            ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
            ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", wt.LONG),
            ("dwFlags", wt.DWORD), ("szExeFile", wt.WCHAR * 260),
        ]

    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE or not snap:
        return -1

    n = 0
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not k32.Process32FirstW(snap, ctypes.byref(e)):
            return 0
        while True:
            if (e.szExeFile.lower() == "msedgewebview2.exe"
                    and e.th32ParentProcessID == parent_pid):
                n += 1
            if not k32.Process32NextW(snap, ctypes.byref(e)):
                break
    finally:
        k32.CloseHandle(snap)
    return n


def capture(hwnd, out_path):
    """PrintWindow + PW_RENDERFULLCONTENT 抓窗口内容（WebView2 必须用这个标志）。"""
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        raise RuntimeError("窗口尺寸异常 %dx%d" % (w, h))

    hdc = user32.GetWindowDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mdc, bmp)
    ok = user32.PrintWindow(hwnd, mdc, PW_RENDERFULLCONTENT)

    class BIH(ctypes.Structure):
        _fields_ = [
            ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
            ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
            ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
            ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
            ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
        ]

    bi = BIH()
    bi.biSize = ctypes.sizeof(BIH)
    bi.biWidth = w
    bi.biHeight = -h
    bi.biPlanes = 1
    bi.biBitCount = 32
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)

    from PIL import Image
    im = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1)
    im.convert("RGB").save(out_path, quality=95)

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(hwnd, hdc)
    return ok, w, h


def main():
    exe = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        TV_RS, "target", "x86_64-pc-windows-msvc", "debug", "clean-tv-app.exe")
    shot = sys.argv[2] if len(sys.argv) > 2 else os.path.join(TV_RS, "_tauri_shot.png")

    if not os.path.isfile(exe):
        print("!! exe 不存在: %s" % exe)
        return 2

    log = open(os.path.join(TV_RS, "_verify_out.txt"), "w", encoding="utf-8")
    proc = subprocess.Popen([exe], cwd=TV_RS, stdout=log, stderr=subprocess.STDOUT)
    print("启动 PID=%d" % proc.pid)

    # 轮询等窗口出现（WebView2 首帧要 1~3 秒）
    win = None
    deadline = time.time() + 20
    while time.time() < deadline:
        if proc.poll() is not None:
            print("!! 进程提前退出，ExitCode=%s" % proc.returncode)
            break
        wins = [x for x in find_windows_by_pid(proc.pid) if x["visible"]]
        if wins:
            win = max(wins, key=lambda x: x["size"][0] * x["size"][1])
            break
        time.sleep(0.4)

    wv2 = count_webview2_children(proc.pid)
    try:
        if win and proc.poll() is None:
            print("窗口: 0x%08X  title=%r  size=%s  class=%s"
                  % (win["hwnd"], win["title"], win["size"], win["cls"]))
            print("WebView2 子进程: %d" % wv2)
            user32.SetForegroundWindow(win["hwnd"])
            # ★ 窗口创建 ≠ 页面就绪：`Tauri Window` 在 WebView2 初始化时就有了，
            #   但页面 JS 执行、IPC 往返、首帧渲染还要 1~3 秒。等太短会拿到空日志，
            #   误判成「前端没跑」。
            time.sleep(3.5)
            # 截图只作留档：判读规则见文件头 —— 白图不代表白屏，别据此下结论。
            ok, w, h = capture(win["hwnd"], shot)
            print("PrintWindow=%s  %dx%d  -> %s（仅供参考，见文件头判读规则）"
                  % (ok, w, h, shot))
        else:
            print("!! 未等到可见窗口")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        log.close()

    txt = ""
    p = os.path.join(TV_RS, "_verify_out.txt")
    if os.path.isfile(p):
        txt = open(p, encoding="utf-8", errors="ignore").read().strip()
    print("--- 进程输出 ---")
    print(txt if txt else "(空)")

    # ★ 成败以页面真实渲染证据为准，而不是「窗口存在」或「截图非白」。
    if "[selfcheck]" in txt:
        print("=> PASS：页面已完成渲染并经 IPC 上报状态")
        return 0
    print("=> FAIL：未收到 [selfcheck] 上报（页面没跑起来，或 IPC 不通）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
"""
端到端播放验证（自动播放钩子版）：启动 app 时设 CLEAN_TV_AUTOPLAY=1，
页面 boot 后自动触发第一个频道播放，等价于「用户点击频道」。判据：
 1. 日志出现 [play] mpv 启动 pid=...（解密+选线+spawn 成功）
 2. 系统里出现 mpv.exe 进程且存活（真实起播，非秒退）

用法：
  python tools/verify_play_e2e.py [exe路径]
"""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TV_RS = os.path.dirname(HERE)

k32 = ctypes.windll.kernel32


def count_proc(name):
    TH32CS_SNAPPROCESS = 0x00000002

    class PE(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                    ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", wt.LONG),
                    ("dwFlags", wt.DWORD), ("szExeFile", wt.WCHAR * 260)]

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap:
        return -1
    n = 0
    try:
        e = PE(); e.dwSize = ctypes.sizeof(PE)
        if not k32.Process32FirstW(snap, ctypes.byref(e)):
            return 0
        while True:
            if e.szExeFile.lower() == name.lower():
                n += 1
            if not k32.Process32NextW(snap, ctypes.byref(e)):
                break
    finally:
        k32.CloseHandle(snap)
    return n


def main():
    exe = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        TV_RS, "target", "x86_64-pc-windows-msvc", "debug", "clean-tv-app.exe")
    if not os.path.isfile(exe):
        print("!! exe 不存在: %s" % exe)
        return 2

    env = dict(os.environ)
    env["CLEAN_TV_AUTOPLAY"] = "1"

    log = open(os.path.join(TV_RS, "_e2e_out.txt"), "w", encoding="utf-8")
    proc = subprocess.Popen([exe], cwd=TV_RS, stdout=log, stderr=subprocess.STDOUT, env=env)
    print("启动 PID=%d（CLEAN_TV_AUTOPLAY=1）" % proc.pid)

    # 轮询：等 mpv 进程出现（上游抓取+解密+起播，启动抓取就要 5~10s，给足 45s）
    played = False
    deadline = time.time() + 45
    while time.time() < deadline:
        if proc.poll() is not None:
            print("!! 进程提前退出 code=%s" % proc.returncode)
            break
        n = count_proc("mpv.exe")
        if n > 0:
            played = True
            break
        time.sleep(0.5)

    # 确认 mpv 存活 3s（非秒退）
    if played:
        time.sleep(3)
        subj = count_proc("mpv.exe")
        print("mpv.exe 进程数=%d（存活 3s）" % subj)
        played = subj > 0

    # 收尾
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    log.close()

    txt = open(os.path.join(TV_RS, "_e2e_out.txt"), encoding="utf-8", errors="ignore").read()
    print("--- 进程输出 ---")
    print(txt if txt else "(空)")

    has_play = "[play] mpv 启动" in txt
    has_sel = "[play] 选中线路" in txt
    print("--- 判定 ---")
    print("解密+起播日志 [play]:", has_play)
    print("选线日志:", has_sel)
    print("mpv 进程存活:", played)
    if has_play and played:
        print("=> PASS：频道 -> 抓取 -> 解密 -> 选线 -> mpv 内嵌起播 全链路贯通")
        return 0
    print("=> FAIL：播放链路未贯通（play日志=%s mpv进程=%s）" % (has_play, played))
    return 1


if __name__ == "__main__":
    sys.exit(main())
# -*- coding: utf-8 -*-
"""exe 启动冒烟：拉起 dist/电视直播.exe，确认窗口出来、进程存活，再关掉。

只验证「能起来」——功能验证走 tools/check_wvt_embed.py（跑源码，同一套代码）。
"""
import os
import json
import sys
import time
import ctypes
import subprocess
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
EXE = os.path.join(ROOT, "desktop", "dist", "电视直播.exe")
SHOT_DIR = os.path.join(ROOT, "_shots")
PY_PIL = r"C:/Users/fengxin/.workbuddy/binaries/python/envs/default/Scripts/python.exe"

u = ctypes.windll.user32
u.GetWindowTextW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
u.GetClassNameW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]

if not os.path.isfile(EXE):
    print("找不到 exe:", EXE)
    sys.exit(1)
print("exe: %s (%.1f MB)" % (EXE, os.path.getsize(EXE) / 1048576.0))

proc = subprocess.Popen([EXE], cwd=os.path.dirname(EXE))
print("已启动 pid=%d，等待 14s（首个 exe 解包会慢）…" % proc.pid)

for i in range(28):
    time.sleep(0.5)
    if proc.poll() is not None:
        print("!! 进程提前退出 rc=%s" % proc.returncode)
        sys.exit(2)

found = [None]


@ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
def cb(hwnd, lp):
    # ★ 不能按 proc.pid 匹配：PyInstaller onefile 的 bootloader 会**再 fork 一个子进程**
    #   来跑真正的 Python，GUI 窗口属于子进程，父进程 pid 找不到它。
    #   按窗口类名 + 标题找最稳。
    cls = ctypes.create_unicode_buffer(128)
    u.GetClassNameW(hwnd, cls, 128)
    if not cls.value.startswith("Tk"):
        return True
    t = ctypes.create_unicode_buffer(256)
    u.GetWindowTextW(hwnd, t, 256)
    if "纯净电视" in (t.value or "") or "电视直播" in (t.value or ""):
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        found[0] = (hwnd, cls.value, t.value, pid.value)
        return False
    return True


deadline = time.time() + 20
while time.time() < deadline and found[0] is None:
    u.EnumWindows(cb, 0)
    if found[0] is None:
        time.sleep(0.5)

ok = found[0] is not None
print(("  [OK] " if ok else "  [!!] ") + "找到主窗口：%s" % (found[0],))

try:
    p = os.path.join(SHOT_DIR, "exe_smoke.png")
    subprocess.run([PY_PIL, "-c",
                    "from PIL import ImageGrab;ImageGrab.grab().save(r'%s')" % p],
                   timeout=25)
    print("  截图 ->", p)
except Exception as e:
    print("  截图失败", e)

# 关掉：先请它自己退（发 WM_CLOSE），2s 内不退就杀
if found[0]:
    WM_CLOSE = 0x0010
    u.PostMessageW(found[0][0], WM_CLOSE, 0, 0)
    for _ in range(8):
        time.sleep(0.25)
        if proc.poll() is not None:
            break
if proc.poll() is None:
    print("  未响应 WM_CLOSE，taskkill")
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                   capture_output=True)

# 确认没有残留的「我们的」浏览器进程 —— v2.4 双路径：
#   ① WebView2 内嵌（msedgewebview2.exe，UDD 是 <exe目录>\.wv2）；
#   ② CDP 嗅探兜底（msedge.exe，profile 是 <exe目录>\.sniff）—— 只在网页线路全败时短暂出现。
# ★ 匹配必须带 exe 所在目录（dist\），否则本机其他副本/项目同名 .sniff
#   profile 的常驻 Edge 会被误判成残留（实测踩过：8 个无关 msedge 导致假 FAIL）。
#   用户自己开着的 Edge/WebView2 更不会用这个目录。
time.sleep(1.5)
DIST = os.path.dirname(EXE)                      # ...\tv_all\desktop\dist
mine = []
for img, mark in (("msedgewebview2.exe", ".wv2"),
                  ("msedge.exe", ".sniff")):
    pat = "*" + DIST + "\\" + mark + "*"
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='%s'\" | "
          "Where-Object { $_.CommandLine -like '%s' } | "
          "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
          % (img, pat))
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, errors="ignore")
    try:
        data = json.loads(r.stdout or "null")
        if isinstance(data, dict):
            data = [data]
    except Exception:
        data = []
    for d in data or []:
        mine.append((img, d.get("ProcessId"),
                     (d.get("CommandLine") or "")[:130]))
print("  我们 profile（dist\\.wv2 / dist\\.sniff）的残留:", mine or "无")
if mine:
    ok = False
    print("  [!!] 有残留浏览器进程")

print("RESULT %s" % ("PASS" if ok else "FAIL"))
sys.exit(0 if ok else 2)

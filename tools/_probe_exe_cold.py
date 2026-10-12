# -*- coding: utf-8 -*-
"""
探针：**模拟「把 exe 发给别人、他双击打开」** —— 全新目录、零缓存。

做四件事：
  ① 把 dist/电视直播.exe 拷到一个空临时目录（= 别人的电脑，没有
     settings.json / fav.json / wvt_*.json / logocache）
  ② 双击（Popen，cwd=该目录），记 t0
  ③ 轮询找主窗口，量「双击 → 窗口出现」的秒数
  ④ 等若干秒截图（肉眼看列表有没有出来），再看该目录落下了什么文件

跑法：
  D:/devtool/python-3.13.0/python.exe tools/_probe_exe_cold.py [观察秒数]
输出：_shots/exe_cold_start.png（+ 控制台时间线）
"""
import os
import sys
import time
import shutil
import ctypes
import tempfile
import subprocess
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
EXE = os.path.join(ROOT, "desktop", "dist", "电视直播.exe")
SHOT_DIR = os.path.join(ROOT, "_shots")
PY_PIL = r"C:/Users/fengxin/.workbuddy/binaries/python/envs/default/Scripts/python.exe"

WATCH = float(sys.argv[1]) if len(sys.argv) > 1 else 22.0

if not os.path.isfile(EXE):
    print("找不到 exe:", EXE)
    sys.exit(1)

# ---- ① 全新的「别人电脑」目录 ----
FRESH = tempfile.mkdtemp(prefix="cleantv_pc_")
TGT = os.path.join(FRESH, "电视直播.exe")
t = time.time()
shutil.copy2(EXE, TGT)
print("exe 已拷到全新目录：%s（%.1fs，%.1f MB）"
      % (FRESH, time.time() - t, os.path.getsize(TGT) / 1048576.0))
print("目录初始内容:", os.listdir(FRESH))

u = ctypes.windll.user32
u.GetWindowTextW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
u.GetClassNameW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]

# ---- ② 双击 ----
T0 = time.time()
proc = subprocess.Popen([TGT], cwd=FRESH)
print("已启动 pid=%d（t0）" % proc.pid)

found = [None]


@ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
def cb(hwnd, lp):
    cls = ctypes.create_unicode_buffer(128)
    u.GetClassNameW(hwnd, cls, 128)
    if not cls.value.startswith("Tk"):
        return True
    tt = ctypes.create_unicode_buffer(256)
    u.GetWindowTextW(hwnd, tt, 256)
    if "纯净电视" in (tt.value or "") or "电视直播" in (tt.value or ""):
        r = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))
        found[0] = (hwnd, cls.value, tt.value,
                    (r.left, r.top, r.right, r.bottom))
        return False
    return True


# ---- ③ 量「窗口出现」----
while time.time() - T0 < 40 and found[0] is None:
    if proc.poll() is not None:
        print("!! 进程提前退出 rc=%s" % proc.returncode)
        sys.exit(2)
    u.EnumWindows(cb, 0)
    if found[0] is None:
        time.sleep(0.2)

t_win = time.time() - T0
print("★ 双击 → 主窗口出现：%.2fs" % t_win)
if found[0]:
    print("   窗口 %s %r rect=%s" % (found[0][1], found[0][2], found[0][3]))
else:
    print("   [!!] 40s 内没找到主窗口")

# ---- ④ 采样 ----
# ★ 采样时刻以「窗口出现」为基准，不以 Popen 为基准：首次双击可能被
#   Defender 全量扫描拖到 30s+（实测过一次 30.17s / 第二次 4.63s），
#   按 Popen 计时会让所有采样点都落在解包阶段，一张有用的都拍不到。
os.makedirs(SHOT_DIR, exist_ok=True)
TWIN = T0 + t_win                       # 窗口出现的绝对时刻
frames = []
for i, at in enumerate((1.5, 3.0, 6.0, WATCH)):
    wait = TWIN + at - time.time()
    if wait > 0:
        time.sleep(wait)
    el_app = time.time() - TWIN
    p = os.path.join(SHOT_DIR, "exe_cold_%02d_app%.1fs.png" % (i + 1, el_app))
    try:
        subprocess.run([PY_PIL, "-c",
                        "from PIL import ImageGrab;ImageGrab.grab().save(r'%s')" % p],
                       timeout=25)
        frames.append((round(el_app, 2), p))
        print("   截图（窗口出现后 %.1fs）-> %s" % (el_app, os.path.basename(p)))
    except Exception as e:
        print("   截图失败：%r" % (e,))

# ---- 全新目录里落下了什么 ----
print()
print("跑完后该目录的内容（= 用户机器上会多出来的东西）：")
for f in sorted(os.listdir(FRESH)):
    fp = os.path.join(FRESH, f)
    if os.path.isfile(fp):
        print("   %-24s %9d bytes" % (f, os.path.getsize(fp)))
    else:
        n = sum(len(fs) for _, _, fs in os.walk(fp))
        print("   %-24s <目录，%d 个文件>" % (f, n))

# ---- 关掉 ----
if found[0]:
    u.PostMessageW(found[0][0], 0x0010, 0, 0)       # WM_CLOSE
    for _ in range(12):
        time.sleep(0.25)
        if proc.poll() is not None:
            break
if proc.poll() is None:
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                   capture_output=True)
print()
print("窗口截图：", [f[1] for f in frames])
print("（全新目录保留：%s）" % FRESH)

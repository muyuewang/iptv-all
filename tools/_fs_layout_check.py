# -*- coding: utf-8 -*-
"""一次性 e2e：进出全屏后左侧频道列表面板必须回到左边（v2.4.2 回归）。
判据：退出全屏后 left_panel.x < right_panel.x，且 left_panel 宽度 ~344、
top_bar 在顶部、ctrl_bar 在底部。
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "desktop"))
import native_app as N  # noqa: E402

FAIL = []


def check(cond, msg):
    print(("  [OK] " if cond else "  [!!] ") + msg)
    if not cond:
        FAIL.append(msg)


def geom(w):
    w.update_idletasks()
    return (w.winfo_x(), w.winfo_y(), w.winfo_width(), w.winfo_height())


def verify_layout(tag):
    lx, ly, lw, lh = geom(app.left_panel)
    rx, ry, rw, rh = geom(app.right_panel)
    tx, ty, _, th = geom(app.top_bar)
    cx, cy, _, ch = geom(app.ctrl_bar)
    print("-- %s: left=(%d,%d,%d,%d) right=(%d,%d,%d,%d) top=(%d,%d,·,%d) ctrl=(%d,%d,·,%d)"
          % (tag, lx, ly, lw, lh, rx, ry, rw, rh, tx, ty, th, cx, cy, ch))
    check(lx < rx, "%s：left_panel 在 right_panel 左边" % tag)
    check(abs(lw - 344) < 40, "%s：left_panel 宽度 %d ≈ 344" % (tag, lw))
    check(ly == 0 and ty == 0, "%s：top_bar/left 贴顶" % tag)
    root_h = app.root.winfo_height()
    check(cy + ch >= root_h - 2, "%s：ctrl_bar 贴底" % tag)
    check(rw > lw, "%s：视频区比频道列表宽" % tag)


app = N.NativeApp()
app.root.geometry("1280x760+40+30")
app.root.attributes("-topmost", True)

# 进官网源再测（更贴近用户实际路径），失败就用本机源
try:
    app._set_src("wvt")
except Exception:
    pass

for _ in range(25):
    app.root.update()
    time.sleep(0.04)

print("== 1) 初始布局 ==")
verify_layout("初始")

print("== 2) 进全屏 ==")
app._toggle_fullscreen()
for _ in range(20):
    app.root.update()
    time.sleep(0.04)
check(app._fullscreen, "已进入全屏")
check(not app.left_panel.winfo_ismapped(), "全屏中 left_panel 已收起")

print("== 3) 退出全屏 ==")
app._toggle_fullscreen()
for _ in range(25):
    app.root.update()
    time.sleep(0.04)
check(not app._fullscreen, "已退出全屏")
verify_layout("退出后")

print("== 4) 再来一轮（复现原 bug 需要二次进出）==")
app._toggle_fullscreen()
for _ in range(15):
    app.root.update()
    time.sleep(0.04)
app._toggle_fullscreen()
for _ in range(25):
    app.root.update()
    time.sleep(0.04)
verify_layout("二次退出后")

app.root.destroy()
print("RESULT %s" % ("FAIL" if FAIL else "PASS"))
os._exit(1 if FAIL else 0)

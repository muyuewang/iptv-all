# -*- coding: utf-8 -*-
"""一次性 e2e：点击频道后的「选中高亮 + 等待动画」反馈链路验证。

流程：起真 app（本机源）→ 点一个频道 → 断言行高亮 + 等待层可见 →
等 mpv 子窗口出现（等待层自动收掉）→ 截图。
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "desktop"))

import native_app as N  # noqa: E402

SHOT = os.path.join(ROOT, "_shots", "ux_loading.png")
os.makedirs(os.path.dirname(SHOT), exist_ok=True)
PY_PIL = r"C:/Users/fengxin/.workbuddy/binaries/python/envs/default/Scripts/python.exe"

FAIL = []


def check(cond, msg):
    print(("  [OK] " if cond else "  [!!] ") + msg)
    sys.stdout.flush()
    if not cond:
        FAIL.append(msg)


app = N.NativeApp()
app.root.geometry("1200x720+60+40")
app.root.update_idletasks()
app.root.update()

ST = {}


def wait_list(n=[0]):
    if app.channels:
        print("== 频道表就绪：%d 个" % len(app.channels))
        c = app.channels[0]
        ST["c"] = c
        key = (str(c["tid"]), str(c["id"]))
        row, name = app._rows.get(key, (None, None))
        check(row is not None, "行控件已注册（key=%s）" % (key,))
        row_bg = row.cget("bg")
        check(row_bg != N.SEL_BG, "未点击前行不高亮（bg=%s）" % row_bg)
        print("== 点击频道：%s" % c["name"])
        app._open_channel(c["tid"], c["id"], c["name"])
        app.root.after(600, after_click)
    else:
        n[0] += 1
        if n[0] > 300:
            check(False, "频道表加载超时")
            finish()
        else:
            app.root.after(200, wait_list)


def after_click():
    c = ST["c"]
    key = (str(c["tid"]), str(c["id"]))
    row, name = app._rows.get(key, (None, None))
    check(row is not None and row.cget("bg") == N.SEL_BG,
          "点击后行高亮为选中色（bg=%s）" % (row.cget("bg") if row else None))
    check(name is not None and name.cget("fg") == N.FG, "选中行名称变为亮色")
    check(app._load_frame.winfo_ismapped(), "等待动画覆盖层已显示")
    check(app._load_name.cget("text") == c["name"], "等待层显示频道名（%s）"
          % app._load_name.cget("text"))
    check("线路" in app._load_base or "获取" in app._load_base,
          "等待层提示连接中（%s）" % app._load_base)
    app.root.update()
    try:
        import subprocess
        subprocess.run([PY_PIL, "-c",
                        "from PIL import ImageGrab;ImageGrab.grab().save(r'%s')" % SHOT],
                       timeout=25)
        print("  截图 -> %s" % SHOT)
    except Exception as e:
        print("  截图失败 %r" % e)
    app.root.after(2500, wait_first_frame)


def wait_first_frame(n=[0]):
    if not app._load_frame.winfo_ismapped():
        child = app._mpv_child()
        check(True, "等待层已收掉（mpv 子窗口出现=%s，用时 %.1fs）"
              % (child, time.time() - ST.get("t0", 0)))
        app.root.after(1500, finish)
        return
    n[0] += 1
    if n[0] > 240:                      # 48s：mpv 网络超时 12s，多留余量
        check(False, "等待层 48s 未收掉（status=%r）" % app.status_lbl.cget("text"))
        return finish()
    app.root.after(200, wait_first_frame)


def finish():
    print()
    check(not FAIL, "全部检查通过")
    print("RESULT %s" % ("FAIL" if FAIL else "PASS"))
    sys.stdout.flush()
    try:
        app._on_close()
    except Exception:
        pass
    N.os._exit(2 if FAIL else 0)


app.root.after(800, wait_list)
app.root.mainloop()
try:
    app._stop_embed(wait=True)
except Exception:
    pass

"""
本机源回归验收：验证「本机源 404 分类 → 回落官网源 → 内嵌 WebView2 嗅探」链路。

★ 与 verify_embed_batch.py 共用同一套判据（屏幕 DC + DPI 感知 + 非黑比例/帧差），
  差别只在于起播源是本机源（local@分类）而不是官网源。

样本覆盖：
  - ys / ws / ty ：app 路由下有播放页（直接解密出线路）
  - tv            ：app 路由 404 → **跨分类重试**（ys/ws/ty 同名）
  - gt / wintv123 ：app 路由 404 → 跨分类也救不回 → **回落官网源**（内嵌嗅探）

用法：
  python tools/verify_local_batch.py            # 全部样本
  python tools/verify_local_batch.py --only 4   # 只跑第 5 个
  python tools/verify_local_batch.py --src local
"""
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from verify_embed_batch import (  # noqa: E402
    analyze,
    belongs_to,
    diff_ratio,
    find_host,
    find_mpv,
    grab_screen,
    pid_of,
    raise_windows,
    rect_of,
    save_png,
    top_window_at,
)

TV_RS = os.path.dirname(HERE)
EXE = os.path.join(TV_RS, "target", "x86_64-pc-windows-msvc", "debug", "clean-tv-app.exe")

# ★ 可用环境变量 CLEAN_TV_EXE 覆盖被测 exe 路径（验收安装目录副本时用）。
if os.environ.get("CLEAN_TV_EXE"):
    EXE = os.environ["CLEAN_TV_EXE"]

# (分类 tid, 期望名称关键词, 说明)
SAMPLES = [
    ("ys", "CCTV1", "本机源·央视（app 路由直解密）"),
    ("ws", "湖南卫视", "本机源·卫视（app 路由直解密）"),
    ("ty", "CCTV5", "本机源·体育（app 路由直解密）"),
    ("tv", "CCTV1", "本机源·综合（404 → 跨分类重试）"),
    ("gt", "凤凰卫视", "本机源·港澳台（404 → 回落官网源嗅探）"),
    ("wintv123", "TVB", "本机源·极速港澳台（404 → 回落官网源嗅探）"),
]


def run_one(tid, timeout=110, idx=0):
    log = os.path.join(TV_RS, "_vlocal_%s.log" % re.sub(r"[^\w]", "_", tid))
    if os.path.exists(log):
        os.remove(log)
    env = dict(os.environ)
    env["CLEAN_TV_LOG"] = log
    env["CLEAN_TV_AUTOPLAY"] = "1"
    env["CLEAN_TV_AUTOPLAY_SRC"] = "local@%s" % tid
    proc = subprocess.Popen([EXE], env=env, cwd=TV_RS)
    t0 = time.time()
    ok_win = False
    ratio = 0.0
    diff = 0.0
    host_rect = None
    best = 0.0
    occluded = 0
    try:
        while time.time() - t0 < timeout:
            time.sleep(1.5)
            mpv = find_mpv(proc.pid)
            if not mpv:
                continue
            ok_win = True
            host = find_host(proc.pid)
            if not host:
                continue
            host_rect = rect_of(host)
            x, y, w, h = host_rect
            # ★ 提到前台 + 确认顶层窗口归属（防抓到遮挡者 → 假阳性）
            raise_windows(proc.pid)
            time.sleep(0.4)
            top = top_window_at(x + w // 2, y + h // 2)
            allowed = {proc.pid, pid_of(mpv)}
            if not belongs_to(top, allowed):
                occluded += 1
                continue
            f1 = grab_screen(x, y, w, h)
            if not f1:
                continue
            time.sleep(1.2)
            f2 = grab_screen(x, y, w, h)
            if not f2:
                continue
            r1, _ = analyze(f1, w, h)
            r2, _ = analyze(f2, w, h)
            diff = diff_ratio(f1, f2)
            best = max(r1, r2)
            ratio = best
            save_png(os.path.join(TV_RS, "_vlocal_%d_f1.png" % idx), f1, w, h)
            save_png(os.path.join(TV_RS, "_vlocal_%d_f2.png" % idx), f2, w, h)
            if best > 0.25 or (diff > 0.01 and best > 0.05):
                break
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    log_lines = []
    if os.path.exists(log):
        with open(log, "r", encoding="utf-8", errors="replace") as f:
            log_lines = [x.strip() for x in f]
    return ok_win, best, diff, log_lines, host_rect, occluded


def main():
    only = None
    if "--only" in sys.argv:
        only = int(sys.argv[sys.argv.index("--only") + 1])
    results = []
    for idx, (tid, kw, desc) in enumerate(SAMPLES):
        if only is not None and idx != only:
            continue
        print("\n===== [%d] 本机源 %s（%s）=====" % (idx, tid, desc))
        t0 = time.time()
        ok_win, ratio, diff, log_lines, hrect, occl = run_one(tid, idx=idx)
        dt = time.time() - t0
        has_picture = (ratio > 0.25) or (diff > 0.01 and ratio > 0.05)
        verdict = "PASS" if (ok_win and has_picture) else "FAIL"

        route = "未起播"
        for ln in log_lines:
            if "按名回落官网源" in ln:
                route = "回落官网源"
            if "命中" in ln and "线路" in ln:
                route = "本机源直解密"
            if "嗅探命中" in ln:
                route = "官网源内嵌嗅探"
            if "跨分类重试" in ln:
                route = "跨分类重试"
        if "回落 789" in "\n".join(log_lines):
            route = "回落789"

        print("  路由: %s" % route)
        print("  承载窗: %s" % (str(hrect) if hrect else "无"))
        print("  出画窗口: %s   非黑像素: %.1f%%   帧间差: %.1f%%   耗时: %.0fs   被遮挡次数: %d" % (
            "有" if ok_win else "无", ratio * 100, diff * 100, dt, occl))
        # 打印关键日志（跳过杂项）
        for ln in log_lines:
            if any(k in ln for k in ("回落", "命中", "嗅探", "出画", "失败", "线路", "无线路")):
                print("    | %s" % ln[:160])
        print("  >>> %s" % verdict)
        results.append((tid, verdict, route, ratio, diff))
    print("\n\n========== 汇总 ==========")
    npass = sum(1 for r in results if r[1] == "PASS")
    for tid, verdict, route, ratio, diff in results:
        print("  %-4s local@%-10s %-16s 非黑 %.0f%% 帧差 %.1f%%" % (
            verdict, tid, route, ratio * 100, diff * 100))
    print("  通过 %d / %d" % (npass, len(results)))
    return 0 if npass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

"""
把 release 产物打成绿色版目录：exe + mpv 资源 + 依赖 dll 一并放入 dist/。

产出：tv_rs/dist/CleanTV-RS/CleanTV-RS.exe + mpv/mpv.exe + mpv/d3dcompiler_43.dll

绿色版无需安装器，双击 exe 即用。mpv 按 resolve_exe_mpv 的优先级
（exe 同目录/mpv/mpv.exe）解析。
"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TV_RS = os.path.dirname(HERE)

def main():
    src_exe = os.path.join(TV_RS, "target", "x86_64-pc-windows-msvc", "release", "clean-tv-app.exe")
    if not os.path.isfile(src_exe):
        print("!! release exe 不存在: %s" % src_exe)
        print("   请先: cargo build --release -p clean-tv-app")
        return 2

    dist = os.path.join(TV_RS, "dist", "CleanTV-RS")
    os.makedirs(dist, exist_ok=True)
    os.makedirs(os.path.join(dist, "mpv"), exist_ok=True)

    # 1) exe
    shutil.copy2(src_exe, os.path.join(dist, "CleanTV-RS.exe"))

    # 2) mpv 资源
    mpv_src = os.path.join(TV_RS, "app", "resources", "mpv")
    for f in ("mpv.exe", "d3dcompiler_43.dll"):
        s = os.path.join(mpv_src, f)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(dist, "mpv", f))

    # 3) 检查 WebView2 运行时：Tauri 依赖系统 WebView2（win10/11 自带），
    #    无需额外捆绑（与桌面版内嵌浏览器不同）。

    print("产出目录: %s" % dist)
    for root, _, files in os.walk(dist):
        for f in files:
            p = os.path.join(root, f)
            print("  %s  (%.1f MB)" % (os.path.relpath(p, dist), os.path.getsize(p) / 1024 / 1024))
    total = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(dist) for f in fs)
    print("总计 %.1f MB" % (total / 1024 / 1024))
    return 0

if __name__ == "__main__":
    sys.exit(main())
